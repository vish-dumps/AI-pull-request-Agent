import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from pydantic import PostgresDsn
from sqlalchemy import delete, text

from app.agents.factory import create_reviewers
from app.db.models import RepositoryIndex
from app.graph.checkpoints import open_postgres_checkpointer
from app.graph.workflow import ReviewWorkflow
from app.services.context_service import ContextService
from app.services.embedding_service import MockEmbeddingProvider
from app.services.indexing_service import IndexingService
from app.services.llm_service import MockLLMProvider


def test_durable_context_resume_uses_saved_snippets_after_index_removal(
    database, settings, multi_job, multi_context
):
    if database.backend != "postgresql":
        pytest.skip("Durable retrieved-context recovery requires PostgreSQL/PGVector")
    configured = settings.model_copy(
        update={"database_url": PostgresDsn(os.environ["TEST_DATABASE_URL"])}
    )
    schema = database.schema + "_workflow"
    embeddings = MockEmbeddingProvider()
    model = MockLLMProvider()
    model.generate = AsyncMock(wraps=model.generate)

    async def run():
        try:
            await IndexingService(database.sessions, embeddings, settings).index_files(
                multi_context.repository,
                multi_context.base_sha,
                {"src/related.py": "def load_user(user_id): return lookup_user(user_id)"},
            )
            async with open_postgres_checkpointer(configured, schema=schema) as saver:
                first = ReviewWorkflow(
                    reviewers=create_reviewers(model, settings),
                    mock_mode=True,
                    sessions=database.sessions,
                    checkpointer=saver,
                    stop_before_persist=True,
                    context_service=ContextService(database.sessions, embeddings, settings),
                )
                await first.start(multi_job, multi_context)
                paused = await first.report(multi_job.review_id)
                assert paused.pending_nodes == ["persist"] and paused.retrieved_context
            async with database.sessions.begin() as session:
                await session.execute(delete(RepositoryIndex))
            embeddings.embed = AsyncMock(side_effect=AssertionError("No embedding on resume"))
            async with open_postgres_checkpointer(configured, schema=schema) as saver:
                second = ReviewWorkflow(
                    reviewers=create_reviewers(model, settings),
                    mock_mode=True,
                    sessions=database.sessions,
                    checkpointer=saver,
                    context_service=ContextService(database.sessions, embeddings, settings),
                )
                await second.resume(multi_job.review_id)
                report = await second.report(multi_job.review_id)
                assert report.status == "COMPLETED" and report.persisted
                assert report.retrieved_context == paused.retrieved_context
                assert model.generate.await_count == 4
                embeddings.embed.assert_not_awaited()
        finally:
            async with database.engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    with asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None
    ) as runner:
        runner.run(run())
