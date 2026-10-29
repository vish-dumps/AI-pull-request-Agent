"""Review normalized PR JSON; optionally persist the single-agent result."""

import argparse
import asyncio
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.agents.security_agent import SecurityAgent
from app.core.config import Settings, get_settings
from app.core.exceptions import LLMProviderError, ReviewConflictError, ReviewStateError
from app.db.session import build_engine, build_session_factory
from app.schemas.github import PullRequestContext
from app.schemas.jobs import ReviewJob
from app.services.llm_service import build_llm_provider
from app.services.review_service import SecurityReviewService


async def review(
    context: PullRequestContext, settings: Settings, persist: bool, review_id: UUID
) -> bool:
    async with build_llm_provider(settings) as provider:
        agent = SecurityAgent(provider, settings)
        if not persist:
            result = await agent.review(context)
            print(result.model_dump_json(indent=2))
            return result.status == "COMPLETED"
        engine = build_engine(settings)
        try:
            job = ReviewJob(
                review_id=review_id,
                delivery_id=uuid5(review_id, "security-cli"),
                repository=context.repository,
                pr_number=context.pr_number,
                action="opened",
                base_sha=context.base_sha,
                head_sha=context.head_sha,
            )
            result = await SecurityReviewService(
                build_session_factory(engine), agent, mock_mode=settings.llm_mock_mode
            ).run(job, context)
            print(result.model_dump_json(indent=2))
            return result.status != "FAILED"
        finally:
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "context",
        type=Path,
        nargs="?",
        default=Path(__file__).resolve().parents[1] / "examples/security_pr.json",
    )
    parser.add_argument("--mock", action="store_true", help="Force deterministic mock mode")
    parser.add_argument(
        "--persist", action="store_true", help="Persist a security-only partial review"
    )
    parser.add_argument("--review-id", type=UUID, help="Reuse a job ID when retrying persistence")
    args = parser.parse_args()
    if args.review_id and not args.persist:
        parser.error("--review-id requires --persist")
    settings = get_settings()
    if args.mock:
        settings = settings.model_copy(update={"llm_mock_mode": True})
    try:
        context = PullRequestContext.model_validate_json(
            args.context.read_text(encoding="utf-8-sig")
        )
        success = asyncio.run(review(context, settings, args.persist, args.review_id or uuid4()))
    except (
        OSError,
        UnicodeError,
        ValidationError,
        LLMProviderError,
        ReviewConflictError,
        ReviewStateError,
    ):
        parser.exit(
            1,
            "Security review could not run: check input, provider config, and job identity.\n",
        )
    except SQLAlchemyError:
        parser.exit(
            1, "Review database unavailable: check DATABASE_URL and run Alembic migrations.\n"
        )
    if not success:
        parser.exit(1)


if __name__ == "__main__":
    main()
