import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import (
    CommentHistoryIncompleteError,
    CommentUnavailableError,
    GitHubAPIError,
    GitHubRateLimitError,
    PublicationConfigurationError,
)
from app.db.models import ReviewPublication
from app.db.repositories import ReviewRepository
from app.schemas.publication import CommentReview, PublicationRead
from app.schemas.publication import PublicationStatus as PS
from app.schemas.review import ReviewStatus
from app.services.comment_renderer import render_comment
from app.services.github_service import GitHubService


def publication_read(row: ReviewPublication) -> PublicationRead:
    return PublicationRead(
        review_id=row.review_id,
        status=row.status,
        attempts=row.attempts,
        post_attempts=row.post_attempts,
        comment_id=row.comment_id,
        error=row.error_message,
        next_retry_at=row.next_retry_at,
        published_at=row.published_at,
    )


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ReviewPublisher:
    """Caller holds the review advisory lock. No transaction spans GitHub I/O."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], github: GitHubService, settings: Settings
    ) -> None:
        self.sessions, self.github, self.settings = sessions, github, settings
        self.destination_hash = hashlib.sha256(
            str(settings.github_api_base_url).rstrip("/").encode()
        ).hexdigest()

    def require_configuration(self) -> None:
        if (
            not self.settings.github_publish_enabled
            or not self.settings.github_token.get_secret_value()
        ):
            raise PublicationConfigurationError(
                "Enable publishing and configure an authenticated GitHub token"
            )

    async def _prepare(self, review_id: UUID) -> tuple[CommentReview, ReviewPublication]:
        async with self.sessions.begin() as session:
            review = await ReviewRepository(session).get_review(review_id)
            if review is None:
                raise LookupError("Review does not exist")
            document = CommentReview.from_stored(review)
            current = await session.get(ReviewPublication, review_id)
            if current is not None:
                return document, current
            if review.status not in {
                ReviewStatus.COMPLETED,
                ReviewStatus.PARTIAL,
                ReviewStatus.FAILED,
            }:
                raise CommentUnavailableError("Review has not finished")
            reason = None
            if review.status == ReviewStatus.FAILED:
                reason = "Failed analysis has no review comment"
            elif review.mock_mode is not False and not self.settings.github_publish_mock_reviews:
                reason = "Mock or unverified legacy reviews are not enabled for publication"
            body = None
            if reason is None:
                try:
                    body = render_comment(document, self.settings)
                except CommentUnavailableError:
                    reason = "Stored review cannot be rendered for publication"
            current = ReviewPublication(
                review_id=review_id,
                status=PS.SKIPPED if reason else PS.PENDING,
                body=body,
                destination_hash=self.destination_hash,
                attempts=0,
                post_attempts=0,
                error_message=reason,
            )
            session.add(current)
            await session.flush()
            return document, current

    async def _save(self, review_id: UUID, **values) -> ReviewPublication:
        async with self.sessions.begin() as session:
            row = await session.get(ReviewPublication, review_id, with_for_update=True)
            if row is None:
                raise LookupError("Publication does not exist")
            for key, value in values.items():
                setattr(row, key, value)
            await session.flush()
            return row

    async def _round(self, row: ReviewPublication) -> tuple[ReviewPublication, bool]:
        if row.status == PS.PENDING:
            if row.attempts >= self.settings.github_publish_max_attempts:
                return await self._save(
                    row.review_id,
                    status=PS.FAILED,
                    next_retry_at=None,
                    error_message="GitHub publication retry budget exhausted",
                ), False
            if row.next_retry_at is not None and aware(row.next_retry_at) > datetime.now(UTC):
                return row, False
        return await self._save(row.review_id, attempts=row.attempts + 1, next_retry_at=None), True

    async def _published(self, row: ReviewPublication, comment_id: int) -> PublicationRead:
        return publication_read(
            await self._save(
                row.review_id,
                status=PS.PUBLISHED,
                comment_id=comment_id,
                published_at=datetime.now(UTC),
                error_message=None,
                next_retry_at=None,
            )
        )

    async def _uncertain(self, row: ReviewPublication) -> PublicationRead:
        return publication_read(
            await self._save(
                row.review_id,
                status=PS.UNCERTAIN,
                error_message=(
                    "Previous comment write has an unknown outcome; "
                    "reconcile before requesting a new review"
                ),
                next_retry_at=None,
            )
        )

    async def _retry(self, row: ReviewPublication, error: GitHubAPIError) -> PublicationRead:
        retryable = (
            error.status_code is None
            or error.status_code >= 500
            or isinstance(error, GitHubRateLimitError)
        ) and not isinstance(error, CommentHistoryIncompleteError)
        delay = min(3600, self.settings.github_publish_retry_seconds * 2 ** (row.attempts - 1))
        if error.retry_after_seconds is not None:
            delay = max(delay, error.retry_after_seconds)
        if isinstance(error, GitHubRateLimitError):
            delay = max(delay, 60 * 2 ** (row.attempts - 1))
        if (
            not retryable
            or row.attempts >= self.settings.github_publish_max_attempts
            or delay > 604800
        ):
            return publication_read(
                await self._save(
                    row.review_id,
                    status=PS.FAILED,
                    error_message=(
                        "GitHub publication failed; check permissions, "
                        "history limits, and retry configuration"
                    ),
                    next_retry_at=None,
                )
            )
        return publication_read(
            await self._save(
                row.review_id,
                status=PS.PENDING,
                error_message="GitHub publication is temporarily unavailable; retry scheduled",
                next_retry_at=datetime.now(UTC) + timedelta(seconds=delay),
            )
        )

    async def _current(self, review: CommentReview) -> bool:
        current = await self.github.get_pull_request(review.repository, review.pr_number)
        return (
            current.state == "open"
            and current.merged is not True
            and current.base.repo is not None
            and current.base.repo.full_name.lower() == review.repository.lower()
            and current.base.sha.lower() == review.base_sha.lower()
            and current.head.sha.lower() == review.head_sha.lower()
        )

    async def publish_owned(self, review_id: UUID) -> PublicationRead:
        self.require_configuration()
        try:
            async with asyncio.timeout(self.settings.github_publish_timeout_seconds):
                return await self._publish_owned(review_id)
        except TimeoutError:
            # A timeout before SENDING is retryable. A timeout after SENDING must
            # retain ambiguity, even when cancellation raced a successful POST.
            async with asyncio.timeout(5):
                async with self.sessions() as session:
                    row = await session.get(ReviewPublication, review_id)
                if row is None:
                    raise
                if row.status in {PS.SENDING, PS.UNCERTAIN}:
                    return await self._uncertain(row)
                if row.status == PS.PENDING:
                    return await self._retry(row, GitHubAPIError("Publication deadline exceeded"))
                return publication_read(row)

    async def _publish_owned(self, review_id: UUID) -> PublicationRead:
        review, row = await self._prepare(review_id)
        if row.status in {PS.PUBLISHED, PS.SKIPPED, PS.FAILED}:
            return publication_read(row)
        recovering = row.status in {PS.SENDING, PS.UNCERTAIN}
        if row.destination_hash != self.destination_hash:
            return publication_read(
                await self._save(
                    review_id,
                    status=PS.UNCERTAIN if recovering else PS.FAILED,
                    error_message="Publication destination differs from the saved attempt",
                    next_retry_at=None,
                )
            )
        row, started = await self._round(row)
        if not started:
            return publication_read(row)
        try:
            actor = await self.github.get_authenticated_user()
            if row.author_id is not None and row.author_id != actor.id:
                return publication_read(
                    await self._save(
                        review_id,
                        status=PS.UNCERTAIN if recovering else PS.FAILED,
                        error_message="Publication credentials differ from the saved author",
                        next_retry_at=None,
                    )
                )
            row = await self._save(review_id, author_id=actor.id)
            if not recovering and not await self._current(review):
                return publication_read(
                    await self._save(
                        review_id,
                        status=PS.SKIPPED,
                        error_message=(
                            "PR is closed, merged, unverifiable, or no longer "
                            "matches the reviewed revision"
                        ),
                    )
                )
            existing = await self.github.find_review_comment(
                review.repository,
                review.pr_number,
                row.body,
                actor.id,
                max_pages=self.settings.github_comment_max_pages,
            )
            if existing is not None:
                return await self._published(row, existing.id)
            if recovering:
                return await self._uncertain(row)
            # A second live read immediately before the write narrows the stale-revision window.
            if not await self._current(review):
                return publication_read(
                    await self._save(
                        review_id,
                        status=PS.SKIPPED,
                        error_message="PR revision changed before publication",
                    )
                )
        except GitHubAPIError as error:
            return await self._uncertain(row) if recovering else await self._retry(row, error)
        row = await self._save(
            review_id,
            status=PS.SENDING,
            post_attempts=row.post_attempts + 1,
            next_retry_at=None,
            error_message=None,
        )
        try:
            data = await self.github.post_review_comment(
                review.repository, review.pr_number, row.body
            )
        except GitHubRateLimitError as error:
            # A definitive 403/429 rejection did not create a comment; another attempt is safe.
            return await self._retry(row, error)
        except GitHubAPIError as error:
            if error.status_code is not None and 300 <= error.status_code < 500:
                return await self._retry(row, error)
            return await self._uncertain(row)
        if type(data.get("id")) is not int or data["id"] <= 0:
            return await self._uncertain(row)
        return await self._published(row, data["id"])
