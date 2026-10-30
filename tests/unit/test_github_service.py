import asyncio
import base64
from copy import deepcopy
from unittest.mock import AsyncMock, call

import httpx
import pytest

from app.core.exceptions import GitHubAPIError, GitHubRateLimitError, PullRequestChangedError
from app.services.github_service import GitHubService, build_github_client


@pytest.fixture
def run_github(settings):
    def run(handler, operation, **overrides):
        configured = settings.model_copy(update=overrides)

        async def invoke():
            async with build_github_client(configured, httpx.MockTransport(handler)) as client:
                return await operation(GitHubService(client, configured))

        return asyncio.run(invoke())

    return run


def file_response(**overrides):
    return {
        "filename": "app/users.py",
        "status": "modified",
        "additions": 8,
        "deletions": 1,
        "patch": "@@ -1 +1,8 @@\n-old\n+new",
        **overrides,
    }


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"retry-after": "120"}, 120),
        ({"x-ratelimit-reset": "1600", "x-ratelimit-remaining": "0"}, 601),
        ({"retry-after": "NaN"}, 60),
        ({"retry-after": "120", "x-ratelimit-reset": "1600", "x-ratelimit-remaining": "0"}, 601),
    ],
)
def test_rate_limit_delay_is_carried_to_worker(headers, expected, monkeypatch):
    monkeypatch.setattr("app.services.github_service.time.time", lambda: 1000)
    assert GitHubService.rate_limit_delay(httpx.Response(429, headers=headers)) == expected


def test_context_paginates_and_preserves_fork_and_missing_patch(run_github, sample_payload):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert request.headers["User-Agent"].startswith("ai-pr-review-agent/")
        assert "authorization" not in request.headers
        if request.url.path.endswith("/files"):
            assert request.url.params["per_page"] == "100"
            if request.url.params["page"] == "1":
                return httpx.Response(
                    200,
                    json=[file_response()],
                    headers={"Link": '<https://attacker.invalid/leak>; rel="next"'},
                )
            return httpx.Response(
                200, json=[file_response(filename="logo.png", patch=None, additions=0, deletions=0)]
            )
        return httpx.Response(200, json=sample_payload["pull_request"])

    context = run_github(
        handler,
        lambda service: service.get_pull_request_context(
            "octocat/Hello-World", 42, expected_head_sha="b" * 40, expected_base_sha="a" * 40
        ),
    )
    assert len(requests) == 4
    assert context.head_repository == "contributor/Hello-World"
    assert context.author == "contributor"
    assert len(context.changed_files) == 2
    assert context.total_additions == 8
    assert not context.files_truncated
    assert len(context.warnings) == 1
    assert context.changed_files[1].patch is None


def test_context_reports_incomplete_file_list_and_null_description(run_github, sample_payload):
    pr = sample_payload["pull_request"]
    pr["body"] = None
    pr["head"]["repo"] = None

    def handler(request):
        return httpx.Response(
            200, json=[file_response()] if request.url.path.endswith("/files") else pr
        )

    context = run_github(
        handler, lambda service: service.get_pull_request_context("octocat/Hello-World", 42)
    )
    assert context.files_truncated
    assert context.description == ""
    assert context.head_repository is None


def test_stale_queued_revision_does_not_fetch_files(run_github, sample_payload):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=sample_payload["pull_request"])

    with pytest.raises(PullRequestChangedError):
        run_github(
            handler,
            lambda service: service.get_pull_request_context(
                "octocat/Hello-World", 42, expected_head_sha="c" * 40
            ),
        )
    assert len(requests) == 1


@pytest.mark.parametrize("branch", ["base", "head"])
def test_pr_change_during_pagination_is_rejected(run_github, sample_payload, branch):
    pr = sample_payload["pull_request"]
    count = 0

    def handler(request):
        nonlocal count
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[file_response()])
        count += 1
        latest = deepcopy(pr)
        if count > 1:
            latest[branch]["sha"] = "c" * 40
        return httpx.Response(200, json=latest)

    with pytest.raises(PullRequestChangedError):
        run_github(
            handler, lambda service: service.get_pull_request_context("octocat/Hello-World", 42)
        )


def test_file_pagination_is_bounded_at_github_limit(run_github):
    calls = []

    def handler(request):
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(
            200,
            json=[file_response(filename=f"{page}-{i}.py") for i in range(100)],
            headers={"Link": '<https://api.github.com/next>; rel="next"'},
        )

    files = run_github(handler, lambda service: service.get_pull_request_files("o/r", 1))
    assert len(files) == 3000
    assert calls == list(range(1, 31))


def test_transient_server_error_uses_bounded_backoff(run_github, monkeypatch, sample_payload):
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.github_service.asyncio.sleep", sleep)
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=sample_payload["pull_request"])

    pr = run_github(handler, lambda service: service.get_pull_request("octocat/Hello-World", 42))
    assert pr.number == 42
    assert len(attempts) == 3
    assert sleep.await_args_list == [call(0.5), call(1.0)]


@pytest.mark.parametrize("failure", ["server", "network"])
def test_exhausted_retry_budget_is_sanitized(run_github, monkeypatch, failure):
    monkeypatch.setattr("app.services.github_service.asyncio.sleep", AsyncMock())
    attempts = []

    def handler(request):
        attempts.append(request)
        if failure == "network":
            raise httpx.ReadTimeout("private-token")
        return httpx.Response(502, text="private-token")

    with pytest.raises(GitHubAPIError) as error:
        run_github(handler, lambda service: service.get_pull_request("o/r", 1))
    assert len(attempts) == 3
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize(
    "status,headers", [(429, {"retry-after": "3600"}), (403, {"x-ratelimit-remaining": "0"})]
)
def test_rate_limits_fail_without_early_retries(run_github, status, headers):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers)

    with pytest.raises(GitHubRateLimitError):
        run_github(handler, lambda service: service.get_pull_request("o/r", 1))
    assert len(requests) == 1


@pytest.mark.parametrize("status", [301, 401, 403, 404, 422])
def test_permanent_failures_are_not_retried(run_github, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://attacker.invalid"})

    with pytest.raises(GitHubAPIError) as error:
        run_github(handler, lambda service: service.get_pull_request("o/r", 1))
    assert error.value.status_code == status
    assert len(requests) == 1


@pytest.mark.parametrize("payload", [{}, [], {"number": "wrong"}])
def test_invalid_response_schema_is_rejected(run_github, payload):
    with pytest.raises(GitHubAPIError):
        run_github(
            lambda request: httpx.Response(200, json=payload),
            lambda service: service.get_pull_request("o/r", 1),
        )


def test_invalid_json_is_rejected(run_github):
    with pytest.raises(GitHubAPIError, match="invalid JSON"):
        run_github(
            lambda request: httpx.Response(200, text="not-json"),
            lambda service: service.get_repository_metadata("o/r"),
        )


def test_repository_metadata_and_pinned_file_content(run_github):
    def handler(request):
        if "/contents/" not in request.url.path:
            return httpx.Response(200, json={"full_name": "o/r", "default_branch": "main"})
        assert request.url.params["ref"] == "a" * 40
        assert request.url.path.endswith("/src/a b.py")
        return httpx.Response(
            200, json={"type": "file", "size": 6, "encoding": "base64", "content": "aGVsbG8K\n"}
        )

    async def operation(service):
        metadata = await service.get_repository_metadata("o/r")
        assert metadata.default_branch == "main"
        return await service.get_file_content("o/r", "src/a b.py", "a" * 40)

    assert run_github(handler, operation) == "hello\n"


@pytest.mark.parametrize("path", ["../secret", "/absolute", "a//b", "a/./b", "a\\b"])
def test_unsafe_file_paths_never_issue_a_request(run_github, path):
    def handler(request):
        pytest.fail("Unsafe path reached GitHub")

    with pytest.raises(ValueError):
        run_github(handler, lambda service: service.get_file_content("o/r", path, "a" * 40))


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "dir"},
        {"size": 1_000_001},
        {"encoding": "none"},
        {"content": "%%%"},
        {"content": base64.b64encode(b"\xff").decode()},
    ],
)
def test_unsupported_file_contents_are_rejected(run_github, overrides):
    payload = {"type": "file", "size": 1, "encoding": "base64", "content": "YQ==", **overrides}
    with pytest.raises(GitHubAPIError):
        run_github(
            lambda request: httpx.Response(200, json=payload),
            lambda service: service.get_file_content("o/r", "a.py", "a" * 40),
        )


def test_decoded_file_size_is_checked_even_if_metadata_is_wrong(run_github):
    payload = {"type": "file", "size": 1, "encoding": "base64", "content": "aGVsbG8="}
    with pytest.raises(GitHubAPIError, match="size limit"):
        run_github(
            lambda request: httpx.Response(200, json=payload),
            lambda service: service.get_file_content("o/r", "a.py", "a" * 40),
            github_max_file_bytes=2,
        )


def test_comment_posts_to_issue_endpoint(run_github):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/repos/o/r/issues/1/comments"
        assert b"Review summary" in request.content
        return httpx.Response(201, json={"id": 123})

    result = run_github(
        handler, lambda service: service.post_review_comment("o/r", 1, "Review summary")
    )
    assert result["id"] == 123


def test_comment_creation_is_not_retried_after_a_network_failure(run_github):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("Request may already have succeeded")

    with pytest.raises(GitHubAPIError):
        run_github(handler, lambda service: service.post_review_comment("o/r", 1, "Review"))
    assert len(requests) == 1


def test_server_retry_after_is_not_ignored(run_github):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, headers={"Retry-After": "120"})

    with pytest.raises(GitHubAPIError, match="delayed retry"):
        run_github(handler, lambda service: service.get_pull_request("o/r", 1))
    assert len(requests) == 1


def test_token_is_sent_only_in_authorization_header(settings):
    from pydantic import SecretStr

    configured = settings.model_copy(update={"github_token": SecretStr("test-private-token")})

    async def run():
        async with build_github_client(configured) as client:
            request = client.build_request("GET", "repos/o/r")
            assert request.headers["Authorization"] == "Bearer test-private-token"
            assert "test-private-token" not in str(request.url)

    asyncio.run(run())
