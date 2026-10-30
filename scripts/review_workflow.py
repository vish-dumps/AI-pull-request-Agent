"""Run or resume the five-stage review graph. Use --mock for a free demo."""

import argparse
import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from app.agents.factory import create_reviewers
from app.agents.security_agent import SecurityAgent
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    EmbeddingError,
    LLMProviderError,
    ReviewConflictError,
    ReviewStateError,
    WorkflowConflictError,
    WorkflowPersistenceError,
)
from app.db.session import build_engine, build_session_factory
from app.graph.checkpoints import open_postgres_checkpointer
from app.graph.workflow import ReviewWorkflow
from app.schemas.github import PullRequestContext
from app.schemas.jobs import ReviewJob
from app.services.context_service import ContextService
from app.services.embedding_service import build_embedding_provider
from app.services.llm_service import build_llm_provider


async def run(
    settings: Settings,
    context: PullRequestContext | None,
    review_id: UUID,
    *,
    persist: bool,
    resume: bool,
    stop_before_persist: bool,
    security_only: bool = False,
) -> bool:
    async with AsyncExitStack() as resources:
        provider = await resources.enter_async_context(build_llm_provider(settings))
        sessions = None
        checkpointer = None
        context_service = None
        if persist:
            engine = build_engine(settings)
            resources.push_async_callback(engine.dispose)
            sessions = build_session_factory(engine)
            checkpointer = await resources.enter_async_context(open_postgres_checkpointer(settings))
        if settings.retrieval_enabled:
            if sessions is None:
                raise WorkflowConflictError("Context retrieval requires --persist")
            embeddings = await resources.enter_async_context(build_embedding_provider(settings))
            context_service = ContextService(sessions, embeddings, settings)
        workflow = ReviewWorkflow(
            SecurityAgent(provider, settings) if security_only else None,
            reviewers=None if security_only else create_reviewers(provider, settings),
            mock_mode=settings.llm_mock_mode,
            sessions=sessions,
            checkpointer=checkpointer,
            stop_before_persist=stop_before_persist,
            context_service=context_service,
        )
        if resume:
            await workflow.resume(review_id)
        else:
            if context is None:
                raise WorkflowConflictError("A new workflow needs PR context")
            job = ReviewJob(
                review_id=review_id,
                delivery_id=uuid5(review_id, "workflow-cli"),
                repository=context.repository,
                pr_number=context.pr_number,
                action="opened",
                base_sha=context.base_sha,
                head_sha=context.head_sha,
            )
            await workflow.start(job, context)
        report = await workflow.report(review_id)
        print(report.model_dump_json(indent=2))
        return report.status != "FAILED"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path, nargs="?")
    parser.add_argument("--mock", action="store_true")
    context_mode = parser.add_mutually_exclusive_group()
    context_mode.add_argument(
        "--with-context", action="store_true", help="Retrieve indexed base-commit context"
    )
    context_mode.add_argument(
        "--diff-only",
        action="store_true",
        help="Disable retrieval, including for legacy checkpoints",
    )
    parser.add_argument(
        "--security-only", action="store_true", help="Use the legacy single-reviewer graph"
    )
    parser.add_argument(
        "--persist", action="store_true", help="Persist reviews and PostgreSQL checkpoints"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--review-id", type=UUID)
    group.add_argument(
        "--resume", type=UUID, help="Resume a durable checkpoint using its review ID"
    )
    parser.add_argument("--stop-before-persist", action="store_true", help="Demo checkpoint pause")
    args = parser.parse_args()
    if args.resume and (not args.persist or args.context or args.stop_before_persist):
        parser.error("--resume requires --persist and cannot take context or --stop-before-persist")
    if args.stop_before_persist and not args.persist:
        parser.error("--stop-before-persist requires durable --persist mode")
    settings = get_settings()
    if args.mock:
        settings = settings.model_copy(update={"llm_mock_mode": True, "embedding_mock_mode": True})
    if args.with_context or args.diff_only:
        settings = settings.model_copy(update={"retrieval_enabled": args.with_context})
    if settings.retrieval_enabled and not args.persist:
        parser.error("Context retrieval requires --persist and a migrated PGVector database")
    try:
        context = None
        if not args.resume:
            fixture = "security_pr.json" if args.security_only else "multi_agent_pr.json"
            path = args.context or Path(__file__).resolve().parents[1] / "examples" / fixture
            context = PullRequestContext.model_validate_json(path.read_text(encoding="utf-8-sig"))
        # Psycopg's async driver needs a selector loop on Windows, not the default proactor.
        with asyncio.Runner(
            loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None
        ) as runner:
            success = runner.run(
                run(
                    settings,
                    context,
                    args.resume or args.review_id or uuid4(),
                    persist=args.persist,
                    resume=args.resume is not None,
                    stop_before_persist=args.stop_before_persist,
                    security_only=args.security_only,
                )
            )
    except (
        OSError,
        UnicodeError,
        ValidationError,
        LLMProviderError,
        EmbeddingError,
        ReviewConflictError,
        ReviewStateError,
        WorkflowConflictError,
        WorkflowPersistenceError,
        LookupError,
    ):
        parser.exit(
            1,
            "Workflow could not run: check input, modes, migrations, and database/checkpoints.\n",
        )
    if not success:
        parser.exit(1)


if __name__ == "__main__":
    main()
