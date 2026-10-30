import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.exceptions import PullRequestChangedError
from app.schemas.github import GitHubPullRequest
from app.services.pr_cache import CachedGitHubService


def test_cache_hit_checks_live_shas_and_refreshes_pr_prose(
    settings, sample_context, sample_payload
):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            github = SimpleNamespace(
                get_pull_request_context=AsyncMock(return_value=sample_context),
                get_pull_request=AsyncMock(
                    return_value=GitHubPullRequest.model_validate(sample_payload["pull_request"])
                ),
            )
            cache = CachedGitHubService(github, redis, settings)
            github.get_pull_request.return_value.changed_files = sample_context.total_changed_files
            args = dict(
                expected_base_sha=sample_context.base_sha, expected_head_sha=sample_context.head_sha
            )
            await cache.get_pull_request_context(
                sample_context.repository, sample_context.pr_number, **args
            )
            github.get_pull_request.return_value.title = "Updated PR title"
            cached = await cache.get_pull_request_context(
                sample_context.repository, sample_context.pr_number, **args
            )
            assert cached.title == "Updated PR title"
            assert github.get_pull_request_context.await_count == 1
            assert github.get_pull_request.await_count == 1
            key = cache.key(
                sample_context.repository,
                sample_context.pr_number,
                sample_context.base_sha,
                sample_context.head_sha,
            )
            assert 0 < await redis.ttl(key) <= settings.pr_cache_ttl_seconds
            github.get_pull_request.return_value.head.sha = "c" * 40
            with pytest.raises(PullRequestChangedError):
                await cache.get_pull_request_context(
                    sample_context.repository, sample_context.pr_number, **args
                )

    asyncio.run(run())


@pytest.mark.parametrize("raw", ["bad JSON", '{"repository":"other/repo"}', "x" * 500001])
def test_corrupt_or_oversized_cache_falls_back_to_github(settings, sample_context, raw):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            github = SimpleNamespace(
                get_pull_request_context=AsyncMock(return_value=sample_context)
            )
            cache = CachedGitHubService(github, redis, settings)
            key = cache.key(
                sample_context.repository,
                sample_context.pr_number,
                sample_context.base_sha,
                sample_context.head_sha,
            )
            await redis.set(key, raw)
            result = await cache.get_pull_request_context(
                sample_context.repository,
                sample_context.pr_number,
                expected_base_sha=sample_context.base_sha,
                expected_head_sha=sample_context.head_sha,
            )
            assert result == sample_context
            github.get_pull_request_context.assert_awaited_once()

    asyncio.run(run())


def test_cache_outage_is_best_effort_and_does_not_fail_review(settings, sample_context):
    async def run():
        redis = SimpleNamespace(
            get=AsyncMock(side_effect=RedisConnectionError()),
            set=AsyncMock(side_effect=RedisConnectionError()),
        )
        github = SimpleNamespace(get_pull_request_context=AsyncMock(return_value=sample_context))
        cache = CachedGitHubService(github, redis, settings)
        result = await cache.get_pull_request_context(
            sample_context.repository,
            sample_context.pr_number,
            expected_base_sha=sample_context.base_sha,
            expected_head_sha=sample_context.head_sha,
        )
        assert result == sample_context

    asyncio.run(run())


def test_cache_keys_isolate_credentials_origin_repository_pr_and_revisions(settings):
    base = CachedGitHubService(None, None, settings).key("demo/repo", 1, "a" * 40, "b" * 40)
    changed = settings.model_copy(update={"github_token": SecretStr("private-token")})
    key = CachedGitHubService(None, None, changed).key("demo/repo", 1, "a" * 40, "b" * 40)
    assert key != base and "private-token" not in key
    changed = settings.model_copy(update={"github_api_base_url": "https://github.example/api/v3/"})
    assert CachedGitHubService(None, None, changed).key("demo/repo", 1, "a" * 40, "b" * 40) != base
    for args in [
        ("other/repo", 1, "a" * 40, "b" * 40),
        ("demo/repo", 2, "a" * 40, "b" * 40),
        ("demo/repo", 1, "c" * 40, "b" * 40),
    ]:
        assert CachedGitHubService(None, None, settings).key(*args) != base
