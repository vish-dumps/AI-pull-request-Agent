from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.factory import create_reviewers
from app.core.config import Settings
from app.core.exceptions import ReviewConflictError, WorkflowPersistenceError
from app.db.repositories import ReviewRepository
from app.graph.checkpoints import open_postgres_checkpointer
from app.graph.workflow import ReviewWorkflow
from app.schemas.jobs import ReviewJob
from app.schemas.review import ReviewStatus
from app.services.context_service import ContextService
from app.services.github_service import PullRequestSource
from app.services.llm_service import LLMProvider
from app.services.worker_queue import TERMINAL
from app.worker.locks import review_lock


class WorkflowProcessor:
    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        provider: LLMProvider,
        github: PullRequestSource,
        context_service: ContextService | None = None,
        checkpointers: Callable[[], AbstractAsyncContextManager[BaseCheckpointSaver]] | None = None,
    ) -> None:
        self.engine, self.sessions, self.settings = engine, sessions, settings
        self.provider, self.github, self.context_service = provider, github, context_service
        self.checkpointers = checkpointers or (lambda: open_postgres_checkpointer(settings))

    def lock(self, job: ReviewJob) -> AbstractAsyncContextManager[bool]:
        return review_lock(self.engine, job.review_id)

    def workflow(self, saver: BaseCheckpointSaver) -> ReviewWorkflow:
        return ReviewWorkflow(
            reviewers=create_reviewers(self.provider, self.settings),
            mock_mode=self.settings.llm_mock_mode,
            sessions=self.sessions,
            github=self.github,
            checkpointer=saver,
            context_service=self.context_service,
        )

    async def terminal_status(self, job: ReviewJob) -> ReviewStatus | None:
        async with self.sessions() as session:
            store = ReviewRepository(session)
            current = await store.get_review(job.review_id)
            if current is None:
                return None
            store._check_job(current, job)
            return current.status if current.status in TERMINAL else None

    @staticmethod
    def check_job(saved: dict, job: ReviewJob) -> None:
        original = ReviewJob.model_validate(saved["job"])
        if original.model_dump(exclude={"created_at"}) != job.model_dump(exclude={"created_at"}):
            raise ReviewConflictError("Checkpoint belongs to a different queued job")

    async def execute(self, job: ReviewJob) -> ReviewStatus:
        async with self.checkpointers() as saver:
            workflow = self.workflow(saver)
            snapshot = await workflow.graph.aget_state(workflow.config(job.review_id))
            if snapshot.values:
                self.check_job(snapshot.values, job)
                await workflow.resume(job.review_id)
            else:
                await workflow.start(job)
            report = await workflow.report(job.review_id)
            if report.paused or not report.persisted or report.status not in TERMINAL:
                raise WorkflowPersistenceError("Worker workflow did not persist a terminal outcome")
            return report.status

    async def fail(self, job: ReviewJob, message: str) -> ReviewStatus:
        """Persist failure when ingestion exists; end pending checkpoints before ACK."""
        persisted = False
        async with self.sessions.begin() as session:
            store = ReviewRepository(session)
            current = await store.get_review(job.review_id)
            if current is not None:
                store._check_job(current, job)
                if current.status in TERMINAL:
                    return current.status
                await store.fail_review(job.review_id, message)
                persisted = True
        async with self.checkpointers() as saver:
            workflow = self.workflow(saver)
            snapshot = await workflow.graph.aget_state(workflow.config(job.review_id))
            if snapshot.values:
                self.check_job(snapshot.values, job)
                await workflow.graph.aupdate_state(
                    workflow.config(job.review_id),
                    {
                        "status": "FAILED",
                        "persisted": persisted,
                        "aggregated_review": None,
                        "errors": [{"stage": "worker", "message": message}],
                    },
                    as_node="persist",
                )
        return ReviewStatus.FAILED
