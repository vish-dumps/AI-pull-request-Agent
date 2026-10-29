import asyncio
import json
from unittest.mock import AsyncMock, call

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

from app.core.exceptions import LLMProviderError
from app.schemas.agents import SecurityReview
from app.services.llm_service import LLMRequest, build_llm_provider


@pytest.fixture
def llm_request():
    return LLMRequest(
        system_prompt="Review security. Output JSON.",
        user_prompt='{"changed_files":[]}',
        schema_name="security_review",
        response_schema=SecurityReview.model_json_schema(),
    )


def envelope(content='{"findings":[],"summary":"No findings","risk_score":0}', **overrides):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}, **overrides}]}


@pytest.fixture
def run_provider(settings, llm_request):
    def run(handler, **overrides):
        configured = settings.model_copy(
            update={
                "llm_mock_mode": False,
                "llm_base_url": HttpUrl("https://model.example/v1"),
                "llm_model": "test-model",
                "llm_api_key": SecretStr("test-private-key"),
                **overrides,
            }
        )

        async def invoke():
            async with build_llm_provider(configured, httpx.MockTransport(handler)) as provider:
                return await provider.generate(llm_request)

        return asyncio.run(invoke())

    return run


def test_compatible_request_uses_schema_model_endpoint_and_auth(run_provider):
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-private-key"
        data = json.loads(request.content)
        assert data["model"] == "test-model"
        assert data["max_completion_tokens"] == 2048
        assert data["response_format"]["json_schema"]["strict"] is True
        assert data["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
        assert "test-private-key" not in request.content.decode()
        return httpx.Response(200, json=envelope())

    output = run_provider(handler)
    assert json.loads(output)["findings"] == []


def test_json_object_compatibility_supplies_schema_in_prompt(run_provider):
    def handler(request):
        data = json.loads(request.content)
        assert data["response_format"] == {"type": "json_object"}
        assert "Required JSON schema:" in data["messages"][0]["content"]
        assert "evidence" in data["messages"][0]["content"]
        assert data["max_tokens"] == 2048
        assert "max_completion_tokens" not in data
        assert "authorization" not in request.headers
        return httpx.Response(200, json=envelope())

    run_provider(
        handler,
        llm_output_mode="json_object",
        llm_token_limit_field="max_tokens",
        llm_api_key=SecretStr(""),
    )


def test_temporary_server_failure_retries_with_backoff(run_provider, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.llm_service.asyncio.sleep", sleep)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502) if len(calls) < 3 else httpx.Response(200, json=envelope())

    run_provider(handler)
    assert len(calls) == 3
    assert sleep.await_args_list == [call(0.5), call(1.0)]


@pytest.mark.parametrize("failure", ["server", "timeout"])
def test_provider_retry_budget_is_bounded_and_errors_are_sanitized(
    run_provider, monkeypatch, failure
):
    monkeypatch.setattr("app.services.llm_service.asyncio.sleep", AsyncMock())
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-provider-response")
        return httpx.Response(503, text="private-provider-response")

    with pytest.raises(LLMProviderError) as error:
        run_provider(handler)
    assert len(calls) == 3
    assert "private-provider-response" not in str(error.value)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 422])
def test_permanent_provider_errors_are_not_retried(run_provider, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private", headers={"Location": "https://evil.invalid"})

    with pytest.raises(LLMProviderError):
        run_provider(handler)
    assert len(calls) == 1


def test_rate_limit_respects_short_retry_after(run_provider, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.llm_service.asyncio.sleep", sleep)
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "2"})
            if len(calls) == 1
            else (httpx.Response(200, json=envelope()))
        )

    run_provider(handler)
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.parametrize("retry_after", ["60", "nan", "inf", "-1", "invalid"])
def test_long_or_invalid_retry_after_defers_without_early_retry(run_provider, retry_after):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after})

    with pytest.raises(LLMProviderError, match="deferred"):
        run_provider(handler)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
        envelope(finish_reason="length"),
        envelope(finish_reason="content_filter"),
        {"choices": [{"finish_reason": "stop", "message": {"refusal": "private-refusal"}}]},
    ],
)
def test_invalid_refused_or_incomplete_responses_are_failures(run_provider, data):
    with pytest.raises(LLMProviderError) as error:
        run_provider(lambda request: httpx.Response(200, json=data))
    assert "private-refusal" not in str(error.value)


def test_invalid_json_envelope_is_a_failure(run_provider):
    with pytest.raises(LLMProviderError, match="invalid response envelope"):
        run_provider(lambda request: httpx.Response(200, text="not-json"))


def test_request_deadline_bounds_stalled_transport(run_provider):
    async def handler(request):
        await asyncio.Event().wait()

    with pytest.raises(LLMProviderError):
        run_provider(handler, llm_request_timeout_seconds=0.01, llm_max_attempts=1)


def test_missing_model_or_base_url_fails_before_network(settings):
    async def run():
        with pytest.raises(LLMProviderError, match="requires"):
            async with build_llm_provider(
                settings.model_copy(
                    update={
                        "llm_mock_mode": False,
                        "llm_model": "",
                        "llm_base_url": None,
                    }
                )
            ):
                pytest.fail("Invalid real provider configuration was accepted")

    asyncio.run(run())
