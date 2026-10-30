import asyncio
import os
from collections import Counter

import pytest
from pydantic import PostgresDsn
from sqlalchemy import text

from app.agents.factory import create_reviewers
from app.db.repositories import ReviewRepository
from app.graph.checkpoints import open_postgres_checkpointer
from app.graph.workflow import ReviewWorkflow
from app.schemas.review import AgentType
from app.services.llm_service import MockLLMProvider


def test_postgres_pending_writes_resume_only_interrupted_reviewer(
    database, settings, multi_job, multi_context
):
    if database.backend != "postgresql":
        pytest.skip("Parallel durable checkpoint recovery requires TEST_DATABASE_URL")
    configured = settings.model_copy(
        update={"database_url": PostgresDsn(os.environ["TEST_DATABASE_URL"])}
    )
    schema = database.schema + "_parallel"
    counts = Counter()

    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        blocked = asyncio.Event()
        for kind, reviewer in reviewers.items():
            original = reviewer.review

            async def review(context, kind=kind, original=original):
                counts[kind] += 1
                if kind == AgentType.QUALITY and counts[kind] == 1:
                    blocked.set()
                    await asyncio.Event().wait()
                return await original(context)

            reviewer.review = review
        try:
            async with open_postgres_checkpointer(configured, schema=schema) as first_saver:
                first = ReviewWorkflow(
                    reviewers=reviewers,
                    mock_mode=True,
                    sessions=database.sessions,
                    checkpointer=first_saver,
                )
                task = asyncio.create_task(first.start(multi_job, multi_context))
                await asyncio.wait_for(blocked.wait(), 2)
                async with asyncio.timeout(5):
                    while True:
                        checkpoint = await first_saver.aget_tuple(first.config(multi_job.review_id))
                        written = {
                            kind
                            for _, channel, value in checkpoint.pending_writes
                            if channel == "agent_results"
                            for kind in value
                        }
                        if written >= {"security", "testing", "documentation"}:
                            break
                        await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            async with open_postgres_checkpointer(configured, schema=schema) as second_saver:
                second = ReviewWorkflow(
                    reviewers=reviewers,
                    mock_mode=True,
                    sessions=database.sessions,
                    checkpointer=second_saver,
                )
                state = await second.resume(multi_job.review_id)
                assert state["status"] == "COMPLETED"
                assert counts[AgentType.QUALITY] == 2
                assert all(counts[kind] == 1 for kind in AgentType if kind != AgentType.QUALITY)
                assert len(state["completed_stages"]) == 8
            async with database.sessions() as session:
                stored = await ReviewRepository(session).get_review(multi_job.review_id)
                assert len(stored.findings) == 4
        finally:
            async with database.engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    with asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None
    ) as runner:
        runner.run(run())
