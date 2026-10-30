import asyncio
import io
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agents.factory import create_reviewers
from app.core.logging import JsonFormatter, log_context
from app.graph.workflow import ReviewWorkflow
from app.services.llm_service import LLMRequest, MockLLMProvider, build_llm_provider


def record(**extras):
    result = logging.LogRecord("test", logging.INFO, "", 0, "safe event", (), None)
    result.__dict__.update(extras)
    return result


def test_context_nests_explicit_fields_win_and_secrets_are_not_structured_fields():
    formatter = JsonFormatter()
    with log_context(review_id="outer", repository="demo/repo", github_token="private-token"):
        with log_context(agent="security"):
            output = formatter.format(
                record(stage="llm_request", attempt=2, authorization="private-token")
            )
            payload = json.loads(output)
            assert payload["review_id"] == "outer" and payload["agent"] == "security"
            assert payload["stage"] == "llm_request" and payload["attempt"] == 2
            assert "private-token" not in output
        assert "agent" not in json.loads(formatter.format(record()))
        assert json.loads(formatter.format(record(review_id="explicit")))["review_id"] == "explicit"
    assert "review_id" not in json.loads(formatter.format(record()))


def test_context_resets_on_failure():
    with pytest.raises(RuntimeError), log_context(review_id="aborted"):
        raise RuntimeError("controlled")
    assert "review_id" not in json.loads(JsonFormatter().format(record()))


def test_provider_retry_keeps_review_and_agent_correlation(settings, monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app.services.llm_service")
    original_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
            ),
        ]
    )
    configured = settings.model_copy(
        update={
            "llm_mock_mode": False,
            "llm_base_url": "https://fixture.invalid/v1/",
            "llm_model": "fixture",
            "llm_max_attempts": 2,
        }
    )
    monkeypatch.setattr("app.services.llm_service.asyncio.sleep", AsyncMock())

    async def run():
        async with build_llm_provider(
            configured, httpx.MockTransport(lambda request: next(responses))
        ) as provider:
            with log_context(
                review_id="review-17", repository="demo/repo", pr_number=17, agent="security"
            ):
                assert (
                    await provider.generate(LLMRequest("system", "{}", "security_review", {}))
                    == "{}"
                )

    try:
        asyncio.run(run())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
    entry = json.loads(stream.getvalue())
    assert entry["review_id"] == "review-17" and entry["agent"] == "security"
    assert entry["stage"] == "llm_request" and entry["attempt"] == 1


def test_parallel_reviews_and_agent_logs_remain_correlated(settings, multi_context, multi_job):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    original_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    async def run():
        async def review(job):
            workflow = ReviewWorkflow(
                reviewers=create_reviewers(MockLLMProvider(), settings), mock_mode=True
            )
            await workflow.start(job, multi_context)

        from uuid import uuid4

        other = multi_job.model_copy(update={"review_id": uuid4()})
        await asyncio.gather(review(multi_job), review(other))
        return {str(multi_job.review_id), str(other.review_id)}

    try:
        expected = asyncio.run(run())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    agents = [row for row in rows if row["message"] == "Specialized review finished"]
    assert len(agents) == 8 and {row["review_id"] for row in agents} == expected
    for review_id in expected:
        group = [row for row in agents if row["review_id"] == review_id]
        assert {row["agent"] for row in group} == {
            "security",
            "quality",
            "testing",
            "documentation",
        }
        assert all(
            row["repository"] == multi_job.repository and row["duration_ms"] >= 0 for row in group
        )
