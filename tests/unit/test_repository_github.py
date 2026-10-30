import asyncio

import httpx
import pytest

from app.core.exceptions import GitHubAPIError
from app.services.github_service import GitHubService, build_github_client


def test_indexing_commit_rejects_mismatched_immutable_revision(settings):
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"sha": "b" * 40, "commit": {"tree": {"sha": "c" * 40}}}
            )
        )
        async with build_github_client(settings, transport) as client:
            with pytest.raises(GitHubAPIError, match="different commit"):
                await GitHubService(client, settings).resolve_repository_commit(
                    "demo/repo", "a" * 40
                )

    asyncio.run(run())


def test_indexing_branch_reference_is_encoded_into_trusted_path(settings):
    def handler(request):
        assert b"feature%2Fauth%23new" in request.url.raw_path
        assert request.url.host == "api.github.com"
        return httpx.Response(200, json={"sha": "a" * 40, "commit": {"tree": {"sha": "c" * 40}}})

    async def run():
        async with build_github_client(settings, httpx.MockTransport(handler)) as client:
            assert await GitHubService(client, settings).resolve_repository_commit(
                "demo/repo", "feature/auth#new"
            ) == ("a" * 40, "c" * 40)

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {"sha": "b" * 40, "truncated": False, "tree": []},
        {"sha": "c" * 40, "truncated": "false", "tree": []},
        {"sha": "c" * 40, "truncated": False, "tree": [{"path": "x.py"}]},
    ],
)
def test_invalid_tree_snapshot_is_rejected(settings, payload):
    async def run():
        async with build_github_client(
            settings, httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client:
            with pytest.raises(GitHubAPIError):
                await GitHubService(client, settings).get_repository_tree("demo/repo", "c" * 40)

    asyncio.run(run())
