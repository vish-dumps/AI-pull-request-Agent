import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.exceptions import EmbeddingError
from app.services.embedding_service import (
    CompatibleEmbeddingProvider,
    MockEmbeddingProvider,
    build_embedding_provider,
    validate_vectors,
)


def configured(settings, **updates):
    return settings.model_copy(
        update={
            "embedding_base_url": "https://embeddings.example/v1/",
            "embedding_model": "user-selected-model",
            "embedding_dimensions": 8,
            "embedding_mock_mode": False,
            **updates,
        }
    )


def test_mock_embeddings_are_free_stable_and_lexically_related():
    async def run():
        provider = MockEmbeddingProvider(128)
        first = await provider.embed(["authenticate user token", "user token"])
        assert first == await provider.embed(["authenticate user token", "user token"])
        assert sum(a * b for a, b in zip(*first, strict=True)) > 0.7
        assert provider.identity != MockEmbeddingProvider(64).identity

    asyncio.run(run())


def test_compatible_embedding_wire_schema_and_response_order(settings):
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/embeddings"
        assert body == {
            "input": ["first", "second"],
            "model": "user-selected-model",
            "encoding_format": "float",
            "dimensions": 8,
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0, 2, 0, 0, 0, 0, 0, 0]},
                    {"index": 0, "embedding": [3, 0, 0, 0, 0, 0, 0, 0]},
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(
            base_url="https://embeddings.example/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            provider = CompatibleEmbeddingProvider(
                client, configured(settings, embedding_send_dimensions=True)
            )
            assert await provider.embed(["first", "second"]) == [
                [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            ]

    asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [0] * 8}],
        [{"index": 1, "embedding": [1] * 8}],
        [{"index": True, "embedding": [1] * 8}],
        [{"index": 0, "embedding": [1] * 7}],
        [{"index": 0, "embedding": [True] * 8}],
    ],
)
def test_bad_embedding_response_fails_without_repair_or_raw_data(settings, data):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": data, "secret": "must-not-escape"})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://embeddings.example/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(EmbeddingError) as error:
                await CompatibleEmbeddingProvider(client, configured(settings)).embed(["source"])
            assert "must-not-escape" not in str(error.value)

    asyncio.run(run())
    assert calls == 1


@pytest.mark.parametrize(
    "vector",
    [
        [float("nan")] * 8,
        [float("inf")] * 8,
        [0.0] * 8,
        [10**400] * 8,
    ],
)
def test_nonfinite_or_zero_vectors_are_rejected(vector):
    with pytest.raises(EmbeddingError):
        validate_vectors([vector], 1, 8)


@pytest.mark.parametrize("status, expected", [(400, 1), (401, 1), (429, 3), (500, 3), (302, 1)])
def test_embedding_retries_are_bounded_and_safe(settings, monkeypatch, status, expected):
    monkeypatch.setattr("app.services.embedding_service.asyncio.sleep", AsyncMock())
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert "dimensions" not in json.loads(request.content)
        return httpx.Response(status, json={"error": "private-source"})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://embeddings.example/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(EmbeddingError) as error:
                await CompatibleEmbeddingProvider(client, configured(settings)).embed(["source"])
            assert "private-source" not in str(error.value)

    asyncio.run(run())
    assert calls == expected


@pytest.mark.parametrize("retry_after", ["60", "NaN", "tomorrow", "-1"])
def test_long_or_invalid_embedding_delays_are_deferred(settings, retry_after):
    async def run():
        async with httpx.AsyncClient(
            base_url="https://embeddings.example/v1/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, headers={"retry-after": retry_after})
            ),
        ) as client:
            with pytest.raises(EmbeddingError, match="deferred"):
                await CompatibleEmbeddingProvider(client, configured(settings)).embed(["source"])

    asyncio.run(run())


def test_real_embeddings_require_explicit_configuration_and_never_fallback(settings):
    async def run():
        with pytest.raises(EmbeddingError, match="require"):
            async with build_embedding_provider(
                settings.model_copy(update={"embedding_mock_mode": False})
            ):
                pytest.fail("No implicit mock fallback")

    asyncio.run(run())


@pytest.mark.parametrize("texts", [[], [""], [" "], ["a" * 8193], ["猫" * 3000], ["x"] * 33])
def test_embedding_input_budget_is_enforced(texts):
    with pytest.raises(EmbeddingError):
        asyncio.run(MockEmbeddingProvider().embed(texts))


def test_embedding_profile_isolates_model_endpoint_dimensions_and_mock(settings):
    async def run():
        async with httpx.AsyncClient() as client:
            original = CompatibleEmbeddingProvider(client, configured(settings)).identity
            for updates in (
                {"embedding_model": "another"},
                {"embedding_dimensions": 16},
                {"embedding_base_url": "https://other.example/v1/"},
            ):
                assert (
                    original
                    != CompatibleEmbeddingProvider(client, configured(settings, **updates)).identity
                )
            assert original != MockEmbeddingProvider(8).identity

    asyncio.run(run())
