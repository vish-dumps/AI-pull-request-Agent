import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.exceptions import LLMProviderError

logger = logging.getLogger(__name__)
MOCK_EVIDENCE = "query = f\"SELECT * FROM users WHERE name = '{username}'\""
MOCK_FILE = "src/users.py"


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    user_prompt: str
    schema_name: str
    response_schema: dict[str, Any]


class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> str:
        """Return raw JSON text; the agent validates every provider's output."""
        ...


class MockLLMProvider:
    async def generate(self, request: LLMRequest) -> str:
        payload = json.loads(request.user_prompt)
        matches_demo = any(
            file["filename"] == MOCK_FILE and MOCK_EVIDENCE in file["patch"]
            for file in payload["changed_files"]
        )
        findings = []
        if matches_demo:
            findings.append(
                {
                    "agent_type": "security",
                    "file_path": MOCK_FILE,
                    "line_number": None,
                    "severity": "HIGH",
                    "title": "Mock example: SQL query includes user input",
                    "description": "Demo finding: username is interpolated into a SQL query.",
                    "suggestion": "Use bound query parameters instead of string interpolation.",
                    "confidence": 0.9,
                    "evidence": MOCK_EVIDENCE,
                }
            )
        return json.dumps(
            {
                "findings": findings,
                "summary": "Mock mode: predefined security response; no model was called.",
                "risk_score": 70 if findings else 0,
            }
        )


class OpenAICompatibleProvider:
    """Chat Completions adapter. The caller owns the injected HTTP client."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        system_prompt = request.system_prompt
        response_format: dict[str, Any] = {"type": self.settings.llm_output_mode}
        if self.settings.llm_output_mode == "json_schema":
            response_format["json_schema"] = {
                "name": request.schema_name,
                "strict": True,
                "schema": request.response_schema,
            }
        else:
            system_prompt += "\nRequired JSON schema:\n" + json.dumps(request.response_schema)
        return {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "response_format": response_format,
            self.settings.llm_token_limit_field: self.settings.llm_max_output_tokens,
        }

    @staticmethod
    def _content(response: httpx.Response) -> str:
        if len(response.content) > 1_000_000:
            raise LLMProviderError("LLM provider response exceeded the size limit")
        try:
            choice = response.json()["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise LLMProviderError("LLM provider refused the review")
            if choice.get("finish_reason") != "stop":
                raise LLMProviderError("LLM output was incomplete or interrupted")
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise LLMProviderError("LLM provider returned empty content")
            return content
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMProviderError("LLM provider returned an invalid response envelope") from None

    async def generate(self, request: LLMRequest) -> str:
        payload = self._payload(request)
        for attempt in range(self.settings.llm_max_attempts):
            delay = 0.5 * (2**attempt)
            try:
                async with asyncio.timeout(self.settings.llm_request_timeout_seconds):
                    response = await self.client.post("chat/completions", json=payload)
            except (httpx.TransportError, TimeoutError):
                if attempt + 1 == self.settings.llm_max_attempts:
                    raise LLMProviderError(
                        "LLM request timed out or failed after bounded attempts"
                    ) from None
            else:
                if 200 <= response.status_code < 300:
                    return self._content(response)
                if response.status_code != 429 and response.status_code < 500:
                    raise LLMProviderError(
                        f"LLM provider rejected the request (HTTP {response.status_code})"
                    )
                if attempt + 1 == self.settings.llm_max_attempts:
                    raise LLMProviderError("LLM provider exhausted the request retry budget")
                if retry_after := response.headers.get("retry-after"):
                    try:
                        requested_delay = float(retry_after)
                    except ValueError:
                        raise LLMProviderError("LLM provider requested a deferred retry") from None
                    if not math.isfinite(requested_delay) or not 0 <= requested_delay <= 5:
                        raise LLMProviderError("LLM provider requested a deferred retry")
                    delay = max(delay, requested_delay)
            logger.warning(
                "Retrying temporary LLM failure",
                extra={"stage": "llm_request", "attempt": attempt + 1},
            )
            await asyncio.sleep(delay)
        raise LLMProviderError("LLM provider request failed")


@asynccontextmanager
async def build_llm_provider(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[LLMProvider]:
    if settings.llm_mock_mode:
        yield MockLLMProvider()
        return
    if settings.llm_base_url is None or not settings.llm_model.strip():
        raise LLMProviderError("Real LLM mode requires LLM_BASE_URL and LLM_MODEL")
    headers = {"Accept": "application/json", "User-Agent": "ai-pr-review-agent/0.1.0"}
    if token := settings.llm_api_key.get_secret_value():
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        base_url=str(settings.llm_base_url).rstrip("/") + "/",
        headers=headers,
        timeout=httpx.Timeout(settings.llm_request_timeout_seconds),
        follow_redirects=False,
        transport=transport,
    ) as client:
        yield OpenAICompatibleProvider(client, settings)
