"""Run or resume the five-stage review graph. Use --mock for a free demo."""

import argparse
import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from app.agents.security_agent import SecurityAgent
from app.core.config import Settings, get_settings
from app.core.exceptions import (
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
from app.services.llm_service import build_llm_provider


async def run(
    settings: Settings,
    context: PullRequestContext | None,
    review_id: UUID,
    *,
    persist: bool,
    resume: bool,
    stop_before_persist: bool,
) -> bool:
    async with AsyncExitStack() as resources:
        provider = await resources.enter_async_context(build_llm_provider(settings))
        sessions = None
        checkpointer = None
        if persist:
            engine = build_engine(settings)
            resources.push_async_callback(engine.dispose)
            sessions = build_session_factory(engine)
            checkpointer = await resources.enter_async_context(open_postgres_checkpointer(settings))
        workflow = ReviewWorkflow(
            SecurityAgent(provider, settings),
            mock_mode=settings.llm_mock_mode,
            sessions=sessions,
            checkpointer=checkpointer,
            stop_before_persist=stop_before_persist,
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
        settings = settings.model_copy(update={"llm_mock_mode": True})
    try:
        context = None
        if not args.resume:
            path = args.context or Path(__file__).resolve().parents[1] / "examples/security_pr.json"
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
                )
            )
    except (
        OSError,
        UnicodeError,
        ValidationError,
        LLMProviderError,
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
