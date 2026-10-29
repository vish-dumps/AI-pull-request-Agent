import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from app.agents.security_agent import SecurityAgent
from app.db.repositories import ReviewRepository
from app.main import create_app
from app.services.llm_service import LLMProvider, MockLLMProvider, build_llm_provider
from app.services.review_service import SecurityReviewService


def test_mock_security_result_is_persisted_and_read_through_api(
    database, settings, resources, security_job, security_context
):
    async def run():
        result = await SecurityReviewService(
            database.sessions, SecurityAgent(MockLLMProvider(), settings), mock_mode=True
        ).run(security_job, security_context)
        assert result.status == "PARTIAL"
        async with database.sessions() as session:
            stored = await ReviewRepository(session).get_review(security_job.review_id)
            assert stored.risk_score == 4.5
            assert stored.summary.startswith("[MOCK] Security-only partial review.")
            assert len(stored.findings) == 1
            assert stored.findings[0].agent_type == "security"

    asyncio.run(run())
    with TestClient(create_app(settings)) as client:
        client.app.state.session_factory = database.sessions
        response = client.get(f"/reviews/{security_job.review_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "PARTIAL"
    assert response.json()["recommendation"] == "REQUEST_CHANGES"
    assert response.json()["findings"][0]["title"].startswith("Mock example")


def test_ingestion_is_committed_before_model_io_and_terminal_retry_skips_model(
    database, settings, security_job, security_context, security_output
):
    provider = Mock(spec=LLMProvider)

    async def generate(request):
        # A separate connection must already see PROCESSING during external work.
        async with database.sessions() as session:
            stored = await ReviewRepository(session).get_review(security_job.review_id)
            assert stored.status == "PROCESSING"
        return json.dumps(security_output)

    provider.generate = AsyncMock(side_effect=generate)

    async def run():
        service = SecurityReviewService(
            database.sessions, SecurityAgent(provider, settings), mock_mode=False
        )
        first = await service.run(security_job, security_context)
        second = await service.run(security_job, security_context)
        assert first.status == second.status == "PARTIAL"
        assert second.reused is True
        assert second.agent_result is None
        provider.generate.assert_awaited_once()

    asyncio.run(run())


def test_invalid_output_is_recorded_as_failed_with_no_findings(
    database, settings, security_job, security_context
):
    provider = Mock(spec=LLMProvider)
    provider.generate = AsyncMock(return_value="private-invalid-LLM-output")

    async def run():
        result = await SecurityReviewService(
            database.sessions, SecurityAgent(provider, settings), mock_mode=False
        ).run(security_job, security_context)
        assert result.status == "FAILED"
        async with database.sessions() as session:
            stored = await ReviewRepository(session).get_review(security_job.review_id)
            assert stored.status == "FAILED"
            assert stored.findings == []
            assert stored.completed_at is not None
            assert "private-invalid" not in stored.error_message

    asyncio.run(run())


def test_compatible_provider_repairs_output_before_database_completion(
    database, settings, security_job, security_context, security_output
):
    calls = []

    def endpoint(request):
        calls.append(json.loads(request.content))
        content = "bad-json" if len(calls) == 1 else json.dumps(security_output)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": content},
                    }
                ]
            },
        )

    configured = settings.model_copy(
        update={
            "llm_mock_mode": False,
            "llm_model": "test-model",
            "llm_base_url": HttpUrl("https://model.example/v1"),
        }
    )

    async def run():
        async with build_llm_provider(configured, httpx.MockTransport(endpoint)) as provider:
            result = await SecurityReviewService(
                database.sessions, SecurityAgent(provider, configured), mock_mode=False
            ).run(security_job, security_context)
        assert result.status == "PARTIAL"
        assert result.agent_result.attempts == 2
        async with database.sessions() as session:
            review = await ReviewRepository(session).get_review(security_job.review_id)
            assert review.risk_score == 4.5
            assert len(review.findings) == 1
        assert len(calls) == 2
        assert "validation_feedback" in json.loads(calls[1]["messages"][1]["content"])

    asyncio.run(run())
