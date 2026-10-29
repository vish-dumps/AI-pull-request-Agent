import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from pydantic import PostgresDsn
from sqlalchemy import text

from app.agents.security_agent import SecurityAgent
from app.db.repositories import ReviewRepository
from app.graph.checkpoints import open_postgres_checkpointer
from app.graph.workflow import ReviewWorkflow
from app.services.llm_service import MockLLMProvider


def test_postgres_checkpoint_survives_connection_and_workflow_recreation(
    database, settings, security_job, security_context
):
    if database.backend != "postgresql":
        pytest.skip("Durable checkpoint recovery requires TEST_DATABASE_URL")
    configured = settings.model_copy(
        update={"database_url": PostgresDsn(os.environ["TEST_DATABASE_URL"])}
    )
    schema = database.schema + "_workflow"
    provider = MockLLMProvider()
    provider.generate = AsyncMock(wraps=provider.generate)

    async def run():
        try:
            async with open_postgres_checkpointer(configured, schema=schema) as first_saver:
                first = ReviewWorkflow(
                    SecurityAgent(provider, settings),
                    mock_mode=True,
                    sessions=database.sessions,
                    checkpointer=first_saver,
                    stop_before_persist=True,
                )
                await first.start(security_job, security_context)
                assert (await first.report(security_job.review_id)).pending_nodes == ["persist"]
            # Both the connection and graph are rebuilt before loading durable state.
            async with open_postgres_checkpointer(configured, schema=schema) as second_saver:
                second = ReviewWorkflow(
                    SecurityAgent(provider, settings),
                    mock_mode=True,
                    sessions=database.sessions,
                    checkpointer=second_saver,
                )
                await second.resume(security_job.review_id)
                report = await second.report(security_job.review_id)
                assert report.persisted
                assert not report.paused
                assert report.status == "PARTIAL"
                assert report.completed_stages == [
                    "ingest",
                    "context",
                    "security",
                    "aggregate",
                    "persist",
                ]
                provider.generate.assert_awaited_once()
            async with database.sessions() as session:
                stored = await ReviewRepository(session).get_review(security_job.review_id)
                assert len(stored.findings) == 1
        finally:
            async with database.engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    with asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None
    ) as runner:
        runner.run(run())
