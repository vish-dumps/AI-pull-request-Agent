import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any
from uuid import UUID

from langgraph.errors import GraphBubbleUp
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.security_agent import SecurityAgent, prepare_security_input
from app.core.exceptions import (
    ReviewConflictError,
    WorkflowPersistenceError,
    WorkflowTransientError,
)
from app.db.repositories import ReviewRepository
from app.graph.state import ReviewState
from app.schemas.agents import SecurityAgentResult
from app.schemas.github import PullRequestContext
from app.schemas.jobs import ReviewJob
from app.schemas.review import ReviewCompletion, ReviewStatus
from app.services.aggregation_service import security_completion
from app.services.github_service import GitHubService

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {ReviewStatus.COMPLETED, ReviewStatus.PARTIAL, ReviewStatus.FAILED}


class ReviewNodes:
    def __init__(
        self,
        agent: SecurityAgent,
        *,
        mock_mode: bool,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        github: GitHubService | None = None,
    ) -> None:
        self.agent = agent
        self.mock_mode = mock_mode
        self.sessions = sessions
        self.github = github

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[ReviewRepository]:
        if self.sessions is None:
            raise WorkflowPersistenceError("Persistence is not configured")
        try:
            async with self.sessions.begin() as session:
                yield ReviewRepository(session)
        except (OperationalError, TimeoutError, OSError):
            raise WorkflowTransientError("Workflow database is temporarily unavailable") from None
        except DBAPIError as error:
            if error.connection_invalidated or getattr(error.orig, "sqlstate", None) in {
                "40001",
                "40P01",
            }:
                raise WorkflowTransientError(
                    "Workflow database transaction must be retried"
                ) from None
            raise WorkflowPersistenceError("Workflow database operation failed") from None
        except SQLAlchemyError:
            raise WorkflowPersistenceError("Workflow database operation failed") from None

    @staticmethod
    def update(stage: str, state: ReviewState, started: float, **changes: Any) -> ReviewState:
        logger.info(
            "Review workflow stage completed",
            extra={
                "review_id": state["review_id"],
                "stage": stage,
                "repository": state["job"]["repository"],
                "pr_number": state["job"]["pr_number"],
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "status": changes.get("status", state.get("status", "PROCESSING")),
            },
        )
        return {"completed_stages": [stage], **changes}

    async def ingest(self, state: ReviewState) -> ReviewState:
        started = perf_counter()
        job = ReviewJob.model_validate(state["job"])
        if state.get("pr_context"):
            context = PullRequestContext.model_validate(state["pr_context"])
        elif self.github is not None:
            context = await self.github.get_pull_request_context(
                job.repository,
                job.pr_number,
                expected_head_sha=job.head_sha,
                expected_base_sha=job.base_sha,
            )
        else:
            raise ReviewConflictError("Ingestion requires PR context or a GitHub service")
        if (job.repository.lower(), job.pr_number, job.base_sha, job.head_sha) != (
            context.repository.lower(),
            context.pr_number,
            context.base_sha,
            context.head_sha,
        ):
            raise ReviewConflictError("Job and PR context do not describe the same revision")
        changes: dict[str, Any] = {
            "pr_context": context.model_dump(mode="json"),
            "status": "PROCESSING",
        }
        if self.sessions is not None:
            async with self.transaction() as store:
                review = await store.create_review(job, context)
                if review.status in TERMINAL_STATUSES:
                    changes.update(status=review.status.value, persisted=True, reused=True)
                else:
                    await store.mark_processing(job.review_id)
        return self.update("ingest", state, started, **changes)

    async def context(self, state: ReviewState) -> ReviewState:
        started = perf_counter()
        original = PullRequestContext.model_validate(state["pr_context"])
        _, patches, warnings = prepare_security_input(original, self.agent.settings)
        prepared = original.model_copy(
            update={
                "changed_files": [
                    file.model_copy(update={"patch": patches[file.filename]})
                    for file in original.changed_files
                    if file.filename in patches
                ],
                "warnings": warnings,
                "files_truncated": original.files_truncated
                or len(patches) < len(original.changed_files),
            }
        )
        return self.update(
            "context",
            state,
            started,
            review_context=prepared.model_dump(mode="json"),
            retrieved_context=[],
            context_source="diff_only",
        )

    async def security(self, state: ReviewState) -> ReviewState:
        started = perf_counter()
        context = PullRequestContext.model_validate(state["review_context"])
        try:
            result = await self.agent.review(context)
        except GraphBubbleUp:
            raise
        except Exception as error:
            # Cancellation propagates; unexpected agent exceptions become safe graph data.
            logger.warning(
                "Security agent raised unexpectedly",
                extra={
                    "review_id": state["review_id"],
                    "stage": "security",
                    "error_type": type(error).__name__,
                },
            )
            result = SecurityAgentResult(
                status="FAILED",
                error="Security agent failed unexpectedly",
                attempts=1,
                duration_ms=(perf_counter() - started) * 1000,
            )
        errors = (
            []
            if result.status == "COMPLETED"
            else [
                {
                    "stage": "security",
                    "message": result.error or "Security review failed",
                }
            ]
        )
        return self.update(
            "security",
            state,
            started,
            security_result=result.model_dump(mode="json"),
            errors=errors,
        )

    async def aggregate(self, state: ReviewState) -> ReviewState:
        started = perf_counter()
        result = SecurityAgentResult.model_validate(state["security_result"])
        review = (
            security_completion(result, mock_mode=self.mock_mode)
            if result.status == "COMPLETED"
            else None
        )
        return self.update(
            "aggregate",
            state,
            started,
            aggregated_review=review.model_dump(mode="json") if review else None,
        )

    async def persist(self, state: ReviewState) -> ReviewState:
        started = perf_counter()
        result = SecurityAgentResult.model_validate(state["security_result"])
        completion = (
            ReviewCompletion.model_validate(state["aggregated_review"])
            if state.get("aggregated_review")
            else None
        )
        status = completion.status if completion else ReviewStatus.FAILED
        if self.sessions is not None:
            async with self.transaction() as store:
                current = await store.get_review(UUID(state["review_id"]))
                if current is None:
                    raise WorkflowPersistenceError("Ingested review is missing")
                if current.status in TERMINAL_STATUSES:
                    # A previous attempt may have committed before its checkpoint write failed.
                    if current.status != status:
                        raise ReviewConflictError(
                            "Stored review disagrees with the checkpoint outcome"
                        )
                elif completion is not None:
                    await store.complete_review(current.id, completion)
                else:
                    await store.fail_review(current.id, result.error or "Security review failed")
        return self.update(
            "persist", state, started, status=status.value, persisted=self.sessions is not None
        )
