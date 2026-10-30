import asyncio

import httpx
import pytest

from app.core.exceptions import CommentHistoryIncompleteError, GitHubAPIError
from app.services.github_service import GitHubService, build_github_client


def test_comment_scan_uses_trusted_paths_and_author_body_matches(settings):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.github.com"
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17, "login": "bot"})
        assert request.url.path == "/repos/demo/repo/issues/1/comments"
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(
                200,
                json=[{"id": 1, "body": "exact", "user": {"id": 999}}],
                headers={"Link": '<https://attacker.invalid/leak>; rel="next"'},
            )
        return httpx.Response(200, json=[{"id": 2, "body": "exact", "user": {"id": 17}}])

    async def run():
        async with build_github_client(settings, httpx.MockTransport(handler)) as client:
            service = GitHubService(client, settings)
            assert (await service.get_authenticated_user()).id == 17
            match = await service.find_review_comment("demo/repo", 1, "exact", 17, max_pages=2)
            assert match.id == 2

    asyncio.run(run())
    assert len(calls) == 3


def test_incomplete_comment_scan_does_not_claim_absence(settings):
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=[], headers={"Link": '<https://evil.invalid>; rel="next"'}
            )
        )
        async with build_github_client(settings, transport) as client:
            with pytest.raises(CommentHistoryIncompleteError):
                await GitHubService(client, settings).find_review_comment(
                    "demo/repo", 1, "body", 17, max_pages=1
                )

    asyncio.run(run())


def test_post_transport_failure_is_never_retried_inline(settings):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private response details")

    async def run():
        async with build_github_client(settings, httpx.MockTransport(handler)) as client:
            with pytest.raises(GitHubAPIError) as error:
                await GitHubService(client, settings).post_review_comment(
                    "demo/repo", 1, "bounded body"
                )
            assert "private response" not in str(error.value)

    asyncio.run(run())
    assert calls == 1
