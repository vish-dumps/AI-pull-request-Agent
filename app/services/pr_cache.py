import asyncio
import hashlib
import logging

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.exceptions import PullRequestChangedError
from app.schemas.github import PullRequestContext
from app.services.github_service import GitHubService

logger = logging.getLogger(__name__)


class CachedGitHubService:
    """Revision-specific processing cache; GitHub remains authoritative on a hit."""

    def __init__(self, github: GitHubService, redis: Redis, settings: Settings) -> None:
        self.github, self.redis, self.settings = github, redis, settings
        namespace = (
            str(settings.github_api_base_url) + "\0" + settings.github_token.get_secret_value()
        )
        self.namespace = hashlib.sha256(namespace.encode()).hexdigest()

    def key(self, repository: str, pr_number: int, base_sha: str, head_sha: str) -> str:
        identity = (
            f"{self.namespace}:{repository.lower()}:{pr_number}:"
            f"{base_sha.lower()}:{head_sha.lower()}"
        )
        return "github:pr:" + hashlib.sha256(identity.encode()).hexdigest()

    async def _get(self, key: str) -> PullRequestContext | None:
        try:
            async with asyncio.timeout(self.settings.redis_operation_timeout_seconds):
                raw = await self.redis.get(key)
            if (
                raw is not None
                and len(raw.encode() if isinstance(raw, str) else raw)
                <= self.settings.pr_cache_max_bytes
            ):
                return PullRequestContext.model_validate_json(raw)
        except (RedisError, OSError, TimeoutError, ValidationError, ValueError):
            logger.warning("PR cache read unavailable or invalid", extra={"stage": "pr_cache"})
        return None

    async def _put(self, key: str, context: PullRequestContext) -> None:
        raw = context.model_dump_json()
        if len(raw.encode()) > self.settings.pr_cache_max_bytes:
            return
        try:
            async with asyncio.timeout(self.settings.redis_operation_timeout_seconds):
                await self.redis.set(key, raw, ex=self.settings.pr_cache_ttl_seconds)
        except (RedisError, OSError, TimeoutError):
            logger.warning("PR cache write unavailable", extra={"stage": "pr_cache"})

    async def get_pull_request_context(
        self,
        repository: str,
        pr_number: int,
        *,
        expected_head_sha: str | None = None,
        expected_base_sha: str | None = None,
    ) -> PullRequestContext:
        if not expected_base_sha or not expected_head_sha:
            return await self.github.get_pull_request_context(
                repository,
                pr_number,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
            )
        key = self.key(repository, pr_number, expected_base_sha, expected_head_sha)
        cached = await self._get(key)
        if cached is not None and (
            cached.repository.lower(),
            cached.pr_number,
            cached.base_sha,
            cached.head_sha,
        ) == (repository.lower(), pr_number, expected_base_sha, expected_head_sha):
            current = await self.github.get_pull_request(repository, pr_number)
            if (current.base.sha, current.head.sha) != (expected_base_sha, expected_head_sha):
                raise PullRequestChangedError("Queued pull request revision is no longer current")
            if current.changed_files == cached.total_changed_files:
                logger.info(
                    "PR context cache lookup", extra={"stage": "pr_cache", "cache_hit": True}
                )
                return cached.model_copy(
                    update={
                        "title": current.title,
                        "description": current.body or "",
                        "author": current.user.login,
                        "base_branch": current.base.ref,
                        "head_branch": current.head.ref,
                        "head_repository": current.head.repo.full_name
                        if current.head.repo
                        else None,
                    }
                )
        logger.info("PR context cache lookup", extra={"stage": "pr_cache", "cache_hit": False})
        context = await self.github.get_pull_request_context(
            repository,
            pr_number,
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
        )
        await self._put(key, context)
        return context
