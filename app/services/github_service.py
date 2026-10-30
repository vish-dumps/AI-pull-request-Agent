import asyncio
import base64
import binascii
import logging
import math
import re
import time
from typing import Protocol, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.config import Settings
from app.core.exceptions import (
    CommentHistoryIncompleteError,
    GitHubAPIError,
    GitHubFileUnsupportedError,
    GitHubRateLimitError,
    PullRequestChangedError,
)
from app.schemas.github import (
    ChangedFile,
    CommitSHA,
    GitHubCommentAuthor,
    GitHubIssueComment,
    GitHubPullRequest,
    GitHubRepository,
    GitTree,
    PullRequestContext,
    validate_repository,
)

logger = logging.getLogger(__name__)
Model = TypeVar("Model", bound=BaseModel)


class PullRequestSource(Protocol):
    async def get_pull_request_context(
        self,
        repository: str,
        pr_number: int,
        *,
        expected_head_sha: str | None = None,
        expected_base_sha: str | None = None,
    ) -> PullRequestContext: ...


def build_github_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": settings.github_api_version,
        "User-Agent": "ai-pr-review-agent/0.1.0",
    }
    if token := settings.github_token.get_secret_value():
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        base_url=str(settings.github_api_base_url).rstrip("/") + "/",
        headers=headers,
        timeout=httpx.Timeout(settings.github_request_timeout_seconds),
        follow_redirects=False,
        transport=transport,
    )


class GitHubService:
    """GitHub REST access. The caller owns and closes the injected HTTP client."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.max_attempts = settings.github_max_attempts
        self.max_file_bytes = settings.github_max_file_bytes

    @staticmethod
    def _pr_path(repository: str, pr_number: int) -> str:
        validate_repository(repository)
        if type(pr_number) is not int or pr_number <= 0:
            raise ValueError("Pull request number must be a positive integer")
        return f"repos/{repository}/pulls/{pr_number}"

    @staticmethod
    def rate_limit_delay(response: httpx.Response) -> float:
        delays = [60.0]
        for header in ("retry-after", "x-ratelimit-reset"):
            value = response.headers.get(header)
            if value is None or (
                header == "x-ratelimit-reset"
                and response.headers.get("x-ratelimit-remaining") != "0"
            ):
                continue
            try:
                delay = float(value)
                if header == "x-ratelimit-reset":
                    delay -= time.time() - 1
            except ValueError:
                continue
            if math.isfinite(delay) and delay >= 0:
                delays.append(delay)
        return max(delays)

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        # GETs are safe to retry. A timed-out POST may already have created a comment.
        attempts = self.max_attempts if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self.client.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise GitHubAPIError("GitHub request failed after bounded attempts") from None
            else:
                if response.status_code == 429 or (
                    response.status_code == 403
                    and (
                        response.headers.get("x-ratelimit-remaining") == "0"
                        or "retry-after" in response.headers
                    )
                ):
                    # Defer the entire job; never hold a worker sleeping for a rate window.
                    raise GitHubRateLimitError(
                        "GitHub API rate limit reached",
                        response.status_code,
                        retry_after_seconds=self.rate_limit_delay(response),
                    )
                if 200 <= response.status_code < 300:
                    return response
                if response.status_code >= 500 and "retry-after" in response.headers:
                    raise GitHubAPIError(
                        "GitHub requested a delayed retry",
                        response.status_code,
                        retry_after_seconds=self.rate_limit_delay(response),
                    )
                if response.status_code < 500 or attempt + 1 == attempts:
                    raise GitHubAPIError("GitHub API request was rejected", response.status_code)
            logger.warning(
                "Retrying temporary GitHub failure",
                extra={"stage": "github_request", "attempt": attempt + 1},
            )
            await asyncio.sleep(0.5 * (2**attempt))
        raise GitHubAPIError("GitHub request failed")  # Defensive: attempts is always positive.

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError:
            raise GitHubAPIError("GitHub returned invalid JSON", response.status_code) from None

    @staticmethod
    def _parse(model: type[Model], payload: object) -> Model:
        try:
            return model.model_validate(payload)
        except ValidationError:
            raise GitHubAPIError("GitHub returned an invalid response schema") from None

    async def get_pull_request(self, repository: str, pr_number: int) -> GitHubPullRequest:
        response = await self._request("GET", self._pr_path(repository, pr_number))
        pull_request = self._parse(GitHubPullRequest, self._json(response))
        if pull_request.number != pr_number or (
            pull_request.base.repo
            and pull_request.base.repo.full_name.lower() != repository.lower()
        ):
            raise GitHubAPIError("GitHub returned a different pull request target")
        return pull_request

    async def get_pull_request_files(self, repository: str, pr_number: int) -> list[ChangedFile]:
        path = self._pr_path(repository, pr_number) + "/files"
        files: list[ChangedFile] = []
        # GitHub exposes at most 3,000 PR files, with at most 100 per page.
        for page in range(1, 31):
            response = await self._request("GET", path, params={"per_page": 100, "page": page})
            payload = self._json(response)
            if not isinstance(payload, list) or len(payload) > 100:
                raise GitHubAPIError("GitHub returned an invalid file list")
            files.extend(self._parse(ChangedFile, file) for file in payload)
            if "next" not in response.links:
                break
            # Only use the presence of the next link; construct the trusted URL locally.
        return files

    async def get_pull_request_context(
        self,
        repository: str,
        pr_number: int,
        *,
        expected_head_sha: str | None = None,
        expected_base_sha: str | None = None,
    ) -> PullRequestContext:
        pr = await self.get_pull_request(repository, pr_number)
        if (expected_head_sha and pr.head.sha != expected_head_sha) or (
            expected_base_sha and pr.base.sha != expected_base_sha
        ):
            raise PullRequestChangedError("Queued pull request revision is no longer current")
        files = await self.get_pull_request_files(repository, pr_number)
        latest = await self.get_pull_request(repository, pr_number)
        if (latest.head.sha, latest.base.sha) != (pr.head.sha, pr.base.sha):
            raise PullRequestChangedError("Pull request changed while fetching its files")
        truncated = pr.changed_files > len(files)
        warnings = []
        if truncated:
            warnings.append("GitHub did not return every changed file; context is incomplete.")
        if any(file.patch is None for file in files):
            warnings.append("Some files have no patch; they may be binary or too large for GitHub.")
        return PullRequestContext(
            repository=repository,
            pr_number=pr.number,
            title=pr.title,
            description=pr.body or "",
            author=pr.user.login,
            base_branch=pr.base.ref,
            head_branch=pr.head.ref,
            base_sha=pr.base.sha,
            head_sha=pr.head.sha,
            head_repository=pr.head.repo.full_name if pr.head.repo else None,
            changed_files=files,
            total_additions=pr.additions,
            total_deletions=pr.deletions,
            total_changed_files=pr.changed_files,
            files_truncated=truncated,
            warnings=warnings,
        )

    async def get_repository_metadata(self, repository: str) -> GitHubRepository:
        validate_repository(repository)
        response = await self._request("GET", f"repos/{repository}")
        return self._parse(GitHubRepository, self._json(response))

    async def get_file_content(self, repository: str, file_path: str, ref: str) -> str:
        validate_repository(repository)
        if (
            not file_path
            or "\\" in file_path
            or any(part in {"", ".", ".."} for part in file_path.split("/"))
        ):
            raise ValueError("File path must be a repository-relative path without traversal")
        if not ref:
            raise ValueError("A file revision is required")
        response = await self._request(
            "GET", f"repos/{repository}/contents/{quote(file_path, safe='/')}", params={"ref": ref}
        )
        data = self._json(response)
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubAPIError("GitHub content is not a regular file")
        size = data.get("size")
        content = data.get("content")
        if (
            type(size) is not int
            or not 0 <= size <= self.max_file_bytes
            or data.get("encoding") != "base64"
            or not isinstance(content, str)
        ):
            raise GitHubFileUnsupportedError(
                "GitHub content is too large or has unsupported encoding"
            )
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
            if len(decoded) > self.max_file_bytes:
                raise GitHubFileUnsupportedError("GitHub content exceeds the file size limit")
            return decoded.decode("utf-8")
        except (binascii.Error, UnicodeError):
            raise GitHubFileUnsupportedError(
                "GitHub content is not a valid UTF-8 text file"
            ) from None

    async def resolve_repository_commit(
        self, repository: str, ref: str | None = None
    ) -> tuple[str, str]:
        validate_repository(repository)
        if ref is None:
            metadata = await self.get_repository_metadata(repository)
            ref = metadata.default_branch
        if not ref or len(ref) > 200:
            raise GitHubAPIError("Repository has no usable revision")
        response = await self._request("GET", f"repos/{repository}/commits/{quote(ref, safe='')}")
        data = self._json(response)
        try:
            sha = TypeAdapter(CommitSHA).validate_python(data["sha"])
            tree_sha = TypeAdapter(CommitSHA).validate_python(data["commit"]["tree"]["sha"])
        except (ValidationError, TypeError, KeyError):
            raise GitHubAPIError("GitHub returned an invalid commit snapshot") from None
        if re.fullmatch(r"[a-fA-F0-9]{40}", ref) and ref.lower() != sha.lower():
            raise GitHubAPIError("GitHub returned a different commit revision")
        return sha.lower(), tree_sha.lower()

    async def get_repository_tree(self, repository: str, tree_sha: str) -> GitTree:
        validate_repository(repository)
        tree_sha = TypeAdapter(CommitSHA).validate_python(tree_sha)
        response = await self._request(
            "GET", f"repos/{repository}/git/trees/{tree_sha}", params={"recursive": "1"}
        )
        if len(response.content) > 10000000:
            raise GitHubAPIError("GitHub tree exceeded the response size limit")
        tree = self._parse(GitTree, self._json(response))
        if tree.sha.lower() != tree_sha.lower():
            raise GitHubAPIError("GitHub returned a different tree revision")
        return tree

    async def post_review_comment(
        self, repository: str, pr_number: int, markdown: str
    ) -> dict[str, object]:
        self._pr_path(repository, pr_number)
        if not markdown.strip():
            raise ValueError("Review comment must not be empty")
        response = await self._request(
            "POST", f"repos/{repository}/issues/{pr_number}/comments", json={"body": markdown}
        )
        data = self._json(response)
        if not isinstance(data, dict) or type(data.get("id")) is not int:
            raise GitHubAPIError("GitHub returned an invalid comment response")
        return data

    async def get_authenticated_user(self) -> GitHubCommentAuthor:
        response = await self._request("GET", "user")
        return self._parse(GitHubCommentAuthor, self._json(response))

    async def find_review_comment(
        self, repository: str, pr_number: int, body: str, author_id: int, *, max_pages: int
    ) -> GitHubIssueComment | None:
        self._pr_path(repository, pr_number)
        for page in range(1, max_pages + 1):
            response = await self._request(
                "GET",
                f"repos/{repository}/issues/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            if len(response.content) > 8000000:
                raise CommentHistoryIncompleteError(
                    "Comment history response exceeded its size limit"
                )
            data = self._json(response)
            if not isinstance(data, list) or len(data) > 100:
                raise GitHubAPIError("GitHub returned an invalid comment list")
            for item in data:
                comment = self._parse(GitHubIssueComment, item)
                if (
                    comment.user is not None
                    and comment.user.id == author_id
                    and comment.body == body
                ):
                    return comment
            if "next" not in response.links:
                return None
        raise CommentHistoryIncompleteError("Comment history scan exceeded the page limit")
