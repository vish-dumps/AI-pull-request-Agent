import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from app.agents.factory import create_reviewers
from app.core.exceptions import WorkflowPersistenceError
from app.db.models import Finding
from app.db.repositories import ReviewRepository
from app.graph.nodes import ReviewNodes
from app.graph.workflow import ReviewWorkflow
from app.main import create_app
from app.schemas.review import AgentType
from app.services.llm_service import MockLLMProvider


@pytest.mark.parametrize(
    "failed_count,expected_status,expected_findings",
    [
        (0, "COMPLETED", 4),
        (1, "PARTIAL", 3),
        (4, "FAILED", 0),
    ],
)
def test_multi_reviewer_outcomes_persist_and_are_readable(
    database,
    settings,
    resources,
    multi_job,
    multi_context,
    failed_count,
    expected_status,
    expected_findings,
):
    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        for kind in list(AgentType)[-failed_count:] if failed_count else []:
            reviewers[kind].review = AsyncMock(side_effect=RuntimeError("private-error"))
        workflow = ReviewWorkflow(reviewers=reviewers, mock_mode=True, sessions=database.sessions)
        result = await workflow.start(multi_job, multi_context)
        assert result["status"] == expected_status
        assert len(result["errors"]) == failed_count
        async with database.sessions() as session:
            stored = await ReviewRepository(session).get_review(multi_job.review_id)
            assert stored.status == expected_status
            assert len(stored.findings) == expected_findings
            if failed_count == 0:
                assert stored.risk_score == 7.7
                assert {finding.agent_type for finding in stored.findings} == set(AgentType)
            if failed_count == 4:
                assert stored.summary is stored.risk is None
                assert "private-error" not in stored.error_message

    asyncio.run(run())
    with TestClient(create_app(settings)) as client:
        client.app.state.session_factory = database.sessions
        response = client.get(f"/reviews/{multi_job.review_id}")
    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    assert len(response.json()["findings"]) == expected_findings
    assert "private-error" not in response.text


def test_full_review_commit_replay_does_not_repeat_any_agent_or_finding(
    database, settings, multi_job, multi_context, monkeypatch
):
    original = ReviewNodes.persist
    attempts = 0

    async def commit_then_fail(self, state):
        nonlocal attempts
        attempts += 1
        update = await original(self, state)
        if attempts == 1:
            raise WorkflowPersistenceError("Simulated post-commit interruption")
        return update

    monkeypatch.setattr(ReviewNodes, "persist", commit_then_fail)
    provider = MockLLMProvider()
    provider.generate = AsyncMock(wraps=provider.generate)
    saver = InMemorySaver()

    async def run():
        first = ReviewWorkflow(
            reviewers=create_reviewers(provider, settings),
            mock_mode=True,
            sessions=database.sessions,
            checkpointer=saver,
        )
        with pytest.raises(WorkflowPersistenceError):
            await first.start(multi_job, multi_context)
        second = ReviewWorkflow(
            reviewers=create_reviewers(provider, settings),
            mock_mode=True,
            sessions=database.sessions,
            checkpointer=saver,
        )
        await second.resume(multi_job.review_id)
        assert (await second.report(multi_job.review_id)).status == "COMPLETED"
        assert provider.generate.await_count == 4
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Finding)) == 4

    asyncio.run(run())
