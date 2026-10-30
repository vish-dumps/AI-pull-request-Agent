import re
from collections.abc import Mapping, Sequence

from app.schemas.agents import AgentResult, SecurityAgentResult
from app.schemas.review import (
    AgentType,
    FindingInput,
    Recommendation,
    ReviewCompletion,
    ReviewStatus,
    Severity,
)

SEVERITY_WEIGHTS = {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 5, Severity.CRITICAL: 10}


def deduplicate_findings(findings: list[FindingInput]) -> list[FindingInput]:
    """Collapse same-file/title findings within three lines; keep the strongest."""
    unique: dict[tuple[str, str], list[FindingInput]] = {}
    for finding in findings:
        title = re.sub(r"[\W_]+", " ", finding.title.casefold()).strip()
        candidates = unique.setdefault((finding.file_path, title), [])
        duplicate = next(
            (
                index
                for index, previous in enumerate(candidates)
                if (previous.line_number is None and finding.line_number is None)
                or (
                    previous.line_number is not None
                    and finding.line_number is not None
                    and abs(previous.line_number - finding.line_number) <= 3
                )
            ),
            None,
        )
        if duplicate is None:
            candidates.append(finding)
        elif (SEVERITY_WEIGHTS[finding.severity], finding.confidence) > (
            SEVERITY_WEIGHTS[candidates[duplicate].severity],
            candidates[duplicate].confidence,
        ):
            candidates[duplicate] = finding
    return sorted(
        (finding for candidates in unique.values() for finding in candidates),
        key=lambda finding: (
            -SEVERITY_WEIGHTS[finding.severity],
            -finding.confidence,
            finding.file_path,
            finding.line_number or 0,
            finding.title,
        ),
    )


def security_completion(result: SecurityAgentResult, *, mock_mode: bool) -> ReviewCompletion:
    if result.review is None or result.status != "COMPLETED":
        raise ValueError("Cannot complete a review with a failed security result")
    findings = deduplicate_findings(
        [
            FindingInput.model_validate(finding.model_dump(exclude={"evidence"}))
            for finding in result.review.findings
        ]
    )
    score = round(sum(SEVERITY_WEIGHTS[f.severity] * f.confidence for f in findings), 2)
    risk = (
        Severity.CRITICAL
        if score >= 8
        else (Severity.HIGH if score >= 4 else Severity.MEDIUM if score >= 1 else Severity.LOW)
    )
    return ReviewCompletion(
        status=ReviewStatus.PARTIAL,
        risk=risk,
        risk_score=score,
        recommendation=Recommendation.REQUEST_CHANGES if score >= 4 else Recommendation.COMMENT,
        summary=("[MOCK] " if mock_mode else "")
        + "Security-only partial review. "
        + result.review.summary
        + " Testing, quality, and documentation agents were not run."
        + (" Coverage was limited." if result.coverage_warnings else ""),
        findings=findings,
    )


def aggregate_reviews(
    results: Mapping[AgentType, AgentResult],
    expected_agents: Sequence[AgentType],
    *,
    mock_mode: bool,
) -> ReviewCompletion | None:
    successful = {
        kind: result
        for kind, result in results.items()
        if kind in expected_agents and result.status == "COMPLETED" and result.review is not None
    }
    if not successful:
        return None
    if tuple(expected_agents) == (AgentType.SECURITY,):
        return security_completion(
            SecurityAgentResult.model_validate(successful[AgentType.SECURITY].model_dump()),
            mock_mode=mock_mode,
        )
    findings = deduplicate_findings(
        [
            FindingInput.model_validate(finding.model_dump(exclude={"evidence"}))
            for kind in AgentType
            if kind in successful
            for finding in successful[kind].review.findings
        ]
    )
    score = round(sum(SEVERITY_WEIGHTS[f.severity] * f.confidence for f in findings), 2)
    risk = (
        Severity.CRITICAL
        if score >= 8
        else (Severity.HIGH if score >= 4 else Severity.MEDIUM if score >= 1 else Severity.LOW)
    )
    missing = [kind.value for kind in AgentType if kind not in successful]
    limited = any(result.coverage_warnings for result in successful.values())
    complete = not missing and not limited
    recommendation = (
        Recommendation.REQUEST_CHANGES
        if score >= 4
        else (Recommendation.COMMENT if findings or not complete else Recommendation.APPROVE)
    )
    summary = ("[MOCK] " if mock_mode else "") + (
        f"{len(successful)}/4 reviewers completed; {len(findings)} actionable finding(s). "
    )
    summary += " ".join(
        f"{kind.value.capitalize()}: {successful[kind].review.summary[:300]}"
        for kind in AgentType
        if kind in successful
    )
    if missing:
        summary += " Incomplete reviewer categories: " + ", ".join(missing) + "."
    if limited:
        summary += " Coverage was limited."
    return ReviewCompletion(
        status=ReviewStatus.COMPLETED if complete else ReviewStatus.PARTIAL,
        risk=risk,
        risk_score=score,
        recommendation=recommendation,
        summary=summary,
        findings=findings,
    )
