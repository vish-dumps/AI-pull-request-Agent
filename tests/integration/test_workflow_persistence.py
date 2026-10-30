"""Workflow side effects, ingestion, and resumable database failures."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.agents.security_agent import SecurityAgent
from app.core.exceptions import WorkflowPersistenceError
from app.db.models import Finding
from app.db.repositories import ReviewRepository
from app.graph.nodes import ReviewNodes
from app.graph.workflow import ReviewWorkflow
from app.services.github_service import GitHubService, build_github_client
from app.services.llm_service import MockLLMProvider


def test_commit_before_checkpoint_failure_replays_without_duplicate_findings(
    database, settings, security_job, security_context, monkeypatch
):
    original = ReviewNodes.persist
    calls = 0

    async def commit_then_fail(self, state):
        nonlocal calls
        calls += 1
        result = await original(self, state)
        if calls == 1:
            raise WorkflowPersistenceError("Simulated crash after database commit")
        return result

    monkeypatch.setattr(ReviewNodes, "persist", commit_then_fail)
    provider = MockLLMProvider()
    provider.generate = AsyncMock(wraps=provider.generate)
    checkpointer = InMemorySaver()

    async def run():
        first = ReviewWorkflow(
            SecurityAgent(provider, settings),
            mock_mode=True,
            sessions=database.sessions,
            checkpointer=checkpointer,
        )
        with pytest.raises(WorkflowPersistenceError):
            await first.start(security_job, security_context)
        assert (await first.report(security_job.review_id)).pending_nodes == ["persist"]
        async with database.sessions() as session:
            stored = await ReviewRepository(session).get_review(security_job.review_id)
            assert stored.status == "PARTIAL"
            assert len(stored.findings) == 1
        second = ReviewWorkflow(
            SecurityAgent(provider, settings),
            mock_mode=True,
            sessions=database.sessions,
            checkpointer=checkpointer,
        )
        await second.resume(security_job.review_id)
        report = await second.report(security_job.review_id)
        assert report.completed_stages == ["ingest", "context", "security", "aggregate", "persist"]
        assert report.persisted
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Finding)) == 1
        provider.generate.assert_awaited_once()

    asyncio.run(run())


def test_transient_database_ingestion_retries_once_without_duplicating_stages(
    database, settings, security_job, security_context, monkeypatch
):
    original = ReviewRepository.create_review
    attempts = 0

    async def flaky(store, job, context, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("SELECT", {}, Exception("private-database-details"))
        return await original(store, job, context, **kwargs)

    monkeypatch.setattr(ReviewRepository, "create_review", flaky)

    async def run():
        workflow = ReviewWorkflow(
            SecurityAgent(MockLLMProvider(), settings), mock_mode=True, sessions=database.sessions
        )
        result = await workflow.start(security_job, security_context)
        assert attempts == 2
        assert result["completed_stages"].count("ingest") == 1
        assert result["status"] == "PARTIAL"

    asyncio.run(run())


def test_exhausted_database_retry_stays_resumable(
    database, settings, security_job, security_context, monkeypatch
):
    original = ReviewRepository.create_review
    failing = AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("private-password")))
    monkeypatch.setattr(ReviewRepository, "create_review", failing)

    async def run():
        workflow = ReviewWorkflow(
            SecurityAgent(MockLLMProvider(), settings), mock_mode=True, sessions=database.sessions
        )
        with pytest.raises(WorkflowPersistenceError) as error:
            await workflow.start(security_job, security_context)
        assert "private-password" not in str(error.value)
        assert failing.await_count == 2
        assert (await workflow.report(security_job.review_id)).pending_nodes == ["ingest"]
        monkeypatch.setattr(ReviewRepository, "create_review", original)
        result = await workflow.resume(security_job.review_id)
        assert result["status"] == "PARTIAL"

    asyncio.run(run())


def test_graph_ingestion_can_fetch_context_through_github_service(
    database, settings, security_job, security_context
):
    requests = []

    def github(request):
        requests.append(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200, json=[file.model_dump() for file in security_context.changed_files]
            )
        return httpx.Response(
            200,
            json={
                "number": security_context.pr_number,
                "title": security_context.title,
                "body": security_context.description,
                "user": {"login": security_context.author},
                "base": {
                    "ref": security_context.base_branch,
                    "sha": security_context.base_sha,
                    "repo": {"full_name": security_context.repository},
                },
                "head": {
                    "ref": security_context.head_branch,
                    "sha": security_context.head_sha,
                    "repo": {"full_name": security_context.head_repository},
                },
                "additions": 2,
                "deletions": 1,
                "changed_files": 1,
            },
        )

    async def run():
        async with build_github_client(settings, httpx.MockTransport(github)) as client:
            workflow = ReviewWorkflow(
                SecurityAgent(MockLLMProvider(), settings),
                mock_mode=True,
                sessions=database.sessions,
                github=GitHubService(client, settings),
            )
            state = await workflow.start(security_job)
        assert len(requests) == 3
        assert state["status"] == "PARTIAL"
        assert state["persisted"]
        assert json.loads(json.dumps(state))["pr_context"]["head_sha"] == security_context.head_sha

    asyncio.run(run())
