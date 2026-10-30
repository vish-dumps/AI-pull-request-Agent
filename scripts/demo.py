"""Generate actual mock workflow reports, Markdown comments, and correlated JSON logs."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import UUID, uuid5

from app.agents.factory import create_reviewers
from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.graph.workflow import ReviewWorkflow
from app.schemas.github import PullRequestContext
from app.schemas.jobs import ReviewJob
from app.schemas.publication import CommentReview
from app.services.comment_renderer import render_comment
from app.services.llm_service import MockLLMProvider

ROOT = Path(__file__).resolve().parents[1]


async def generate(output_directory: Path):
    await asyncio.to_thread(output_directory.mkdir, parents=True, exist_ok=True)
    settings = Settings.model_construct()
    context = PullRequestContext.model_validate_json(
        (ROOT / "examples/multi_agent_pr.json").read_bytes()
    )
    reports = []
    with (output_directory / "events.jsonl").open("w", encoding="utf-8") as log_file:
        handler = logging.StreamHandler(log_file)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("app")
        previous_level, previous_propagate = logger.level, logger.propagate
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            for name, limit, suffix in (("review", 30, "0011"), ("limited", 1, "0012")):
                configured = settings.model_copy(update={"max_files_per_review": limit})
                review_id = UUID("00000000-0000-4000-8000-00000000" + suffix)
                job = ReviewJob(
                    review_id=review_id,
                    delivery_id=uuid5(review_id, "phase11-demo"),
                    repository=context.repository,
                    pr_number=context.pr_number,
                    action="opened",
                    base_sha=context.base_sha,
                    head_sha=context.head_sha,
                )
                workflow = ReviewWorkflow(
                    reviewers=create_reviewers(MockLLMProvider(), configured), mock_mode=True
                )
                await workflow.start(job, context)
                report = await workflow.report(review_id)
                if report.review is None:
                    raise RuntimeError("Mock demo did not produce a review")
                document = CommentReview(
                    review_id=review_id,
                    repository=context.repository,
                    pr_number=context.pr_number,
                    base_sha=context.base_sha,
                    head_sha=context.head_sha,
                    mock_mode=True,
                    **report.review.model_dump(),
                )
                (output_directory / f"{name}.json").write_text(
                    report.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
                (output_directory / f"{name}.md").write_text(
                    render_comment(document, configured), encoding="utf-8"
                )
                reports.append(
                    {
                        "name": name,
                        "status": report.status,
                        "findings": len(report.review.findings),
                        "risk_score": report.review.risk_score,
                        "recommendation": report.review.recommendation,
                    }
                )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate
    return {
        "mode": "mock_demo",
        "persisted": False,
        "published": False,
        "reports": reports,
        "output_directory": str(output_directory),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/demo"))
    args = parser.parse_args()
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    print(json.dumps(asyncio.run(generate(args.output_dir)), indent=2))


if __name__ == "__main__":
    main()
