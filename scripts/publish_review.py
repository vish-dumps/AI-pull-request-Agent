"""Preview a saved review comment, or explicitly publish/reconcile it. --demo is free."""

import argparse
import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import UUID, uuid4

from app.agents.factory import create_reviewers
from app.core.config import get_settings
from app.db.models import ReviewPublication
from app.db.repositories import ReviewRepository
from app.db.session import build_engine, build_session_factory
from app.graph.workflow import ReviewWorkflow
from app.schemas.github import PullRequestContext
from app.schemas.jobs import ReviewJob
from app.schemas.publication import CommentReview
from app.services.comment_renderer import render_comment
from app.services.github_service import GitHubService, build_github_client
from app.services.llm_service import MockLLMProvider
from app.services.publication_service import ReviewPublisher
from app.worker.locks import review_lock

DEMO_PATH = Path(__file__).resolve().parents[1] / "examples/multi_agent_pr.json"


async def run(args: argparse.Namespace) -> bool:
    settings = get_settings()
    if args.demo:
        context = PullRequestContext.model_validate_json(
            await asyncio.to_thread(DEMO_PATH.read_bytes)
        )
        job = ReviewJob(
            review_id=UUID("00000000-0000-4000-8000-000000000009"),
            delivery_id=uuid4(),
            repository=context.repository,
            pr_number=context.pr_number,
            action="opened",
            base_sha=context.base_sha,
            head_sha=context.head_sha,
        )
        workflow = ReviewWorkflow(
            reviewers=create_reviewers(MockLLMProvider(), settings), mock_mode=True
        )
        await workflow.start(job, context)
        report = await workflow.report(job.review_id)
        document = CommentReview(
            review_id=job.review_id,
            repository=context.repository,
            pr_number=context.pr_number,
            base_sha=context.base_sha,
            head_sha=context.head_sha,
            mock_mode=True,
            **report.review.model_dump(),
        )
        print(render_comment(document, settings))
        return True
    async with AsyncExitStack() as resources:
        engine = build_engine(settings)
        resources.push_async_callback(engine.dispose)
        sessions = build_session_factory(engine)
        if not args.publish:
            async with sessions() as session:
                review = await ReviewRepository(session).get_review(args.review_id)
                if review is None:
                    raise LookupError("Review does not exist")
                row = await session.get(ReviewPublication, args.review_id)
                body = (
                    row.body
                    if row is not None and row.body is not None
                    else render_comment(CommentReview.from_stored(review), settings)
                )
                print(body)
                return True
        async with review_lock(engine, args.review_id) as locked:
            if not locked:
                raise ValueError("Review is owned by another caller")
            client = await resources.enter_async_context(build_github_client(settings))
            publisher = ReviewPublisher(sessions, GitHubService(client, settings), settings)
            result = await publisher.publish_owned(args.review_id)
            print(result.model_dump_json(indent=2))
            return result.status in {"PUBLISHED", "SKIPPED"}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_id", type=UUID, nargs="?")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Render the predefined fixture without services or GitHub calls",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Write only if enabled; unknown writes are reconciled read-only",
    )
    args = parser.parse_args()
    if bool(args.review_id) == bool(args.demo) or (args.demo and args.publish):
        parser.error("Choose REVIEW_UUID [--publish] or --demo")
    try:
        success = asyncio.run(run(args))
    except KeyboardInterrupt:
        parser.exit(
            1, "Publication interrupted; reconcile the saved attempt before another write.\n"
        )
    except Exception:
        parser.exit(
            1,
            "Comment could not be prepared or published; check review, migrations, "
            "publishing configuration, and ownership.\n",
        )
    if not success:
        parser.exit(1)


if __name__ == "__main__":
    main()
