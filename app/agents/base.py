import json
import logging
import re
from time import perf_counter

from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import LLMProviderError
from app.schemas.agents import AgentResult, AgentReview
from app.schemas.context import RetrievedChunk
from app.schemas.github import PullRequestContext
from app.schemas.review import AgentType
from app.services.llm_service import LLMProvider, LLMRequest

logger = logging.getLogger(__name__)
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_line_numbers(patch: str) -> set[int]:
    numbers = set()
    line_number = None
    for line in patch.splitlines():
        if match := HUNK_HEADER.match(line):
            line_number = int(match.group(1))
        elif line_number is not None:
            if line.startswith("+"):
                numbers.add(line_number)
                line_number += 1
            elif line.startswith(" "):
                line_number += 1
    return numbers


def prepare_review_input(
    context: PullRequestContext,
    settings: Settings,
    retrieved_context: list[RetrievedChunk] | None = None,
) -> tuple[dict[str, object], dict[str, str], list[str]]:
    warnings = [warning[:300] for warning in context.warnings[:10]]
    if context.files_truncated or context.total_changed_files > len(context.changed_files):
        warnings.append("Some changed files were not supplied for review.")
    patches: dict[str, str] = {}
    files = []
    remaining = settings.max_patch_chars
    missing_patches = sum(not file.patch for file in context.changed_files)
    if missing_patches:
        warnings.append(f"{missing_patches} changed file(s) have no textual patch.")
    for changed in context.changed_files:
        if not changed.patch:
            continue
        if len(changed.filename) > 1024:
            warnings.append("A file path exceeded the prompt input limit and was skipped.")
            continue
        if len(files) >= settings.max_files_per_review or remaining <= 0:
            warnings.append("Review limited by the configured file/patch budget.")
            break
        patch = changed.patch
        if len(patch) > remaining:
            patch = patch[:remaining].rsplit("\n", 1)[0] if "\n" in patch[:remaining] else ""
            warnings.append(f"Patch truncated for {changed.filename}.")
        if patch:
            files.append({"filename": changed.filename, "status": changed.status, "patch": patch})
            patches[changed.filename] = patch
            remaining -= len(patch)
    if len(context.title) > 500 or len(context.description) > 4000:
        warnings.append("PR title/description was truncated for the prompt.")
    warnings = list(dict.fromkeys(warning[:300] for warning in warnings))[:10]
    payload = {
        "repository": context.repository,
        "pr_number": context.pr_number,
        "title": context.title[:500],
        "description": context.description[:4000],
        "base_sha": context.base_sha,
        "head_sha": context.head_sha,
        "changed_files": files,
        "coverage_warnings": warnings,
    }
    if retrieved_context:
        snippets = []
        remaining = settings.max_context_chars
        for chunk in retrieved_context[: settings.max_context_chunks]:
            if (chunk.repository.lower(), chunk.commit_sha.lower()) != (
                context.repository.lower(),
                context.base_sha.lower(),
            ):
                raise ValueError("Retrieved context does not match the PR base snapshot")
            if remaining <= 0:
                break
            text = chunk.content[:remaining]
            end_line = max(
                chunk.start_line, chunk.start_line + text.count("\n") - int(text.endswith("\n"))
            )
            snippets.append(
                {**chunk.model_dump(mode="json"), "content": text, "end_line": end_line}
            )
            remaining -= len(text)
        payload["retrieved_context"] = snippets
    return payload, patches, warnings


def check_finding_evidence(review: AgentReview, patches: dict[str, str]) -> None:
    for finding in review.findings:
        patch = patches.get(finding.file_path)
        if patch is None:
            raise ValueError("Finding cites a file outside the supplied patches")
        code = "\n".join(
            line[1:]
            for line in patch.splitlines()
            if line.startswith(("+", "-", " ")) and not line.startswith(("+++ ", "--- "))
        )
        if finding.evidence not in code:
            raise ValueError("Finding evidence is absent from the cited patch")
        if finding.line_number is not None and finding.line_number not in added_line_numbers(patch):
            raise ValueError("Finding line number is not an added line in the supplied patch")


class ReviewAgent:
    agent_type: AgentType
    system_prompt: str
    review_schema: type[AgentReview]
    result_schema: type[AgentResult]

    def __init__(self, provider: LLMProvider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings

    async def review(
        self, context: PullRequestContext, *, retrieved_context: list[RetrievedChunk] | None = None
    ) -> AgentResult:
        started = perf_counter()
        label = self.agent_type.value.capitalize()
        payload, patches, warnings = prepare_review_input(context, self.settings, retrieved_context)
        if not patches:
            result = self.result_schema(
                status="COMPLETED",
                review=self.review_schema(
                    findings=[],
                    risk_score=0,
                    summary=f"No textual diff was available for {self.agent_type.value} review.",
                ),
                attempts=0,
                duration_ms=(perf_counter() - started) * 1000,
                coverage_warnings=warnings + ["No textual patches were reviewed."],
            )
            self._log_result(result, context)
            return result
        for attempt in range(1, 3):
            request = LLMRequest(
                system_prompt=self.system_prompt,
                user_prompt=json.dumps(payload),
                schema_name=f"{self.agent_type.value}_review",
                response_schema=self.review_schema.model_json_schema(),
            )
            try:
                raw = await self.provider.generate(request)
            except LLMProviderError as error:
                return self._failure(str(error), attempt, started, warnings, context)
            try:
                review = self.review_schema.model_validate_json(raw)
                check_finding_evidence(review, patches)
            except (ValidationError, ValueError) as error:
                # Never echo untrusted output or Pydantic input values into logs/feedback.
                feedback = "Evidence, filename, or line citation did not match the supplied diff."
                if isinstance(error, ValidationError):
                    issues = error.errors(
                        include_input=False, include_context=False, include_url=False
                    )
                    feedback = "Schema validation failed: " + "; ".join(
                        issue["type"] for issue in issues[:5]
                    )
                payload["validation_feedback"] = (
                    feedback + " Return corrected JSON; do not invent issues."
                )
                if attempt == 2:
                    return self._failure(
                        f"{label} output failed validation after one repair attempt",
                        attempt,
                        started,
                        warnings,
                        context,
                    )
                logger.warning(
                    "Reviewer output failed validation; requesting one repair",
                    extra={
                        "stage": f"{self.agent_type.value}_agent",
                        "agent": self.agent_type.value,
                        "repository": context.repository,
                        "pr_number": context.pr_number,
                        "attempt": attempt,
                    },
                )
                continue
            result = self.result_schema(
                status="COMPLETED",
                review=review,
                attempts=attempt,
                duration_ms=(perf_counter() - started) * 1000,
                coverage_warnings=warnings,
            )
            self._log_result(result, context)
            return result
        raise RuntimeError("Unreachable agent review state")

    def _failure(
        self,
        error: str,
        attempts: int,
        started: float,
        warnings: list[str],
        context: PullRequestContext,
    ) -> AgentResult:
        result = self.result_schema(
            status="FAILED",
            error=error,
            attempts=attempts,
            duration_ms=(perf_counter() - started) * 1000,
            coverage_warnings=warnings,
        )
        self._log_result(result, context)
        return result

    def _log_result(self, result: AgentResult, context: PullRequestContext) -> None:
        logger.info(
            "Specialized review finished",
            extra={
                "stage": f"{self.agent_type.value}_agent",
                "agent": self.agent_type.value,
                "repository": context.repository,
                "pr_number": context.pr_number,
                "status": result.status,
                "duration_ms": round(result.duration_ms, 2),
            },
        )
