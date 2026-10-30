"""Small embedding boundary; no provider SDK or paid calls in mock mode."""

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.exceptions import EmbeddingError

MAX_EMBEDDING_BYTES = 8192


class EmbeddingProvider(Protocol):
    identity: str
    dimensions: int
    mock_mode: bool

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def validate_vectors(vectors: object, count: int, dimensions: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != count:
        raise EmbeddingError("Embedding response count does not match input")
    result = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise EmbeddingError("Embedding response has incompatible dimensions")
        try:
            valid = all(type(value) in {int, float} and math.isfinite(value) for value in vector)
            norm = math.hypot(*vector) if valid else 0
        except (OverflowError, TypeError, ValueError):
            valid, norm = False, 0
        if not valid:
            raise EmbeddingError("Embedding response contains invalid vector values")
        if not math.isfinite(norm) or norm == 0:
            raise EmbeddingError("Embedding response contains a zero or invalid vector")
        result.append([float(value / norm) for value in vector])
    return result


def validate_inputs(texts: list[str]) -> None:
    if (
        not texts
        or len(texts) > 32
        or any(
            not text.strip() or len(text.encode("utf-8")) > MAX_EMBEDDING_BYTES for text in texts
        )
    ):
        raise EmbeddingError("Embedding inputs exceed the bounded batch/text limits")


class MockEmbeddingProvider:
    """Deterministic lexical feature hashing, not semantic model embeddings."""

    mock_mode = True

    def __init__(self, dimensions: int = 128) -> None:
        self.dimensions = dimensions
        self.identity = f"mock-lexical-v1:{dimensions}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        validate_inputs(texts)
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())
            for word in words or [text]:
                digest = hashlib.sha256(word.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1
            vectors.append(vector)
        return validate_vectors(vectors, len(texts), self.dimensions)


class CompatibleEmbeddingProvider:
    mock_mode = False

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        if settings.embedding_base_url is None or not settings.embedding_model.strip():
            raise EmbeddingError("Real embeddings require EMBEDDING_BASE_URL and EMBEDDING_MODEL")
        self.client = client
        self.settings = settings
        self.dimensions = settings.embedding_dimensions
        profile = [str(settings.embedding_base_url), settings.embedding_model, self.dimensions]
        self.identity = "compatible-v1:" + hashlib.sha256(json.dumps(profile).encode()).hexdigest()

    def _vectors(self, response: httpx.Response, count: int) -> list[list[float]]:
        try:
            if len(response.content) > 4000000:
                raise EmbeddingError("Embedding response exceeds the size limit")
            data = response.json()["data"]
            if not isinstance(data, list) or len(data) != count:
                raise ValueError
            ordered = [None] * count
            for entry in data:
                index = entry["index"]
                if type(index) is not int or not 0 <= index < count or ordered[index] is not None:
                    raise ValueError
                ordered[index] = entry["embedding"]
            return validate_vectors(ordered, count, self.dimensions)
        except (ValueError, KeyError, TypeError, IndexError):
            raise EmbeddingError("Embedding provider returned an invalid response") from None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        validate_inputs(texts)
        payload = {
            "model": self.settings.embedding_model,
            "input": texts,
            "encoding_format": "float",
        }
        if self.settings.embedding_send_dimensions:
            payload["dimensions"] = self.dimensions
        for attempt in range(self.settings.embedding_max_attempts):
            delay = 0.5 * 2**attempt
            try:
                async with asyncio.timeout(self.settings.embedding_request_timeout_seconds):
                    response = await self.client.post("embeddings", json=payload)
            except (httpx.TransportError, TimeoutError):
                if attempt + 1 == self.settings.embedding_max_attempts:
                    raise EmbeddingError(
                        "Embedding request failed after bounded attempts"
                    ) from None
            else:
                if 200 <= response.status_code < 300:
                    return self._vectors(response, len(texts))
                if response.status_code != 429 and response.status_code < 500:
                    raise EmbeddingError("Embedding provider rejected the request")
                if value := response.headers.get("retry-after"):
                    try:
                        delay = float(value)
                    except ValueError:
                        raise EmbeddingError(
                            "Embedding provider requested a deferred retry"
                        ) from None
                    if not math.isfinite(delay) or not 0 <= delay <= 5:
                        raise EmbeddingError("Embedding provider requested a deferred retry")
                if attempt + 1 == self.settings.embedding_max_attempts:
                    raise EmbeddingError("Embedding request failed after bounded attempts")
            await asyncio.sleep(delay)
        raise EmbeddingError("Embedding request failed")


@asynccontextmanager
async def build_embedding_provider(settings: Settings) -> AsyncIterator[EmbeddingProvider]:
    if settings.embedding_mock_mode:
        yield MockEmbeddingProvider(settings.embedding_dimensions)
        return
    if settings.embedding_base_url is None or not settings.embedding_model.strip():
        raise EmbeddingError("Real embeddings require EMBEDDING_BASE_URL and EMBEDDING_MODEL")
    headers = {}
    if key := settings.embedding_api_key.get_secret_value():
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(
        base_url=str(settings.embedding_base_url).rstrip("/") + "/",
        headers=headers,
        timeout=settings.embedding_request_timeout_seconds,
        follow_redirects=False,
    ) as client:
        yield CompatibleEmbeddingProvider(client, settings)
