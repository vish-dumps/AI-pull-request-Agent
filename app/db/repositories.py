from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.exceptions import ReviewConflictError, ReviewStateError
from app.db.models import Finding, PullRequest, Repository, Review
from app.schemas.github import PullRequestContext, validate_repository
from app.schemas.jobs import ReviewJob
from app.schemas.review import ReviewCompletion, ReviewStatus


class ReviewRepository:
    """Persistence operations. Callers own the transaction; these methods never commit."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _repository(self, name: str) -> Repository:
        name = validate_repository(name).lower()
        query = select(Repository).where(Repository.github_full_name == name)
        repository = await self.session.scalar(query)
        if repository is None:
            try:
                async with self.session.begin_nested():
                    repository = Repository(github_full_name=name)
                    self.session.add(repository)
                    await self.session.flush()
            except IntegrityError:
                # A concurrent ingestion may have created the same repository.
                repository = await self.session.scalar(query)
                if repository is None:
                    raise
        return repository

    async def _pull_request(
        self, repository: Repository, context: PullRequestContext
    ) -> PullRequest:
        query = (
            select(PullRequest)
            .where(
                PullRequest.repository_id == repository.id,
                PullRequest.pr_number == context.pr_number,
            )
            .with_for_update()
        )
        pull_request = await self.session.scalar(query)
        if pull_request is None:
            try:
                async with self.session.begin_nested():
                    pull_request = PullRequest(
                        repository_id=repository.id,
                        pr_number=context.pr_number,
                        title=context.title,
                        author=context.author,
                        base_branch=context.base_branch,
                        head_branch=context.head_branch,
                    )
                    self.session.add(pull_request)
                    await self.session.flush()
            except IntegrityError:
                pull_request = await self.session.scalar(query)
                if pull_request is None:
                    raise
        pull_request.title = context.title
        pull_request.author = context.author
        pull_request.base_branch = context.base_branch
        pull_request.head_branch = context.head_branch
        repository.updated_at = datetime.now(UTC)
        return pull_request

    @staticmethod
    def _check_job(review: Review, job: ReviewJob) -> None:
        if (
            review.delivery_id != job.delivery_id
            or review.base_sha != job.base_sha
            or review.head_sha != job.head_sha
            or review.pull_request.pr_number != job.pr_number
            or review.pull_request.repository.github_full_name != job.repository.lower()
        ):
            raise ReviewConflictError("Review ID already belongs to different inputs")

    async def create_review(self, job: ReviewJob, context: PullRequestContext) -> Review:
        if (
            job.repository.lower() != context.repository.lower()
            or job.pr_number != context.pr_number
            or job.base_sha != context.base_sha
            or job.head_sha != context.head_sha
        ):
            raise ReviewConflictError("Job and PR context do not describe the same revision")
        if existing := await self.get_review(job.review_id):
            self._check_job(existing, job)
            return existing
        repository = await self._repository(context.repository)
        pull_request = await self._pull_request(repository, context)
        try:
            async with self.session.begin_nested():
                review = Review(
                    id=job.review_id,
                    delivery_id=job.delivery_id,
                    pull_request_id=pull_request.id,
                    status=ReviewStatus.QUEUED,
                    base_sha=job.base_sha,
                    head_sha=job.head_sha,
                    created_at=job.created_at,
                )
                self.session.add(review)
                await self.session.flush()
        except IntegrityError:
            existing = await self.get_review(job.review_id)
            if existing is None:
                raise ReviewConflictError(
                    "Delivery is already assigned to another review"
                ) from None
            self._check_job(existing, job)
            return existing
        return await self.get_review(job.review_id)

    async def get_review(self, review_id: UUID) -> Review | None:
        query = (
            select(Review)
            .where(Review.id == review_id)
            .options(
                joinedload(Review.pull_request).joinedload(PullRequest.repository),
                selectinload(Review.findings),
            )
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(query)

    async def list_repository_reviews(
        self, repository: str, *, limit: int = 20, offset: int = 0
    ) -> list[Review]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid review pagination")
        name = validate_repository(repository).lower()
        query = (
            select(Review)
            .join(Review.pull_request)
            .join(PullRequest.repository)
            .where(Repository.github_full_name == name)
            .options(joinedload(Review.pull_request).joinedload(PullRequest.repository))
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self.session.scalars(query))

    async def _lock_review(self, review_id: UUID) -> Review:
        query = (
            select(Review)
            .where(Review.id == review_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        review = await self.session.scalar(query)
        if review is None:
            raise LookupError("Review does not exist")
        return review

    async def mark_processing(self, review_id: UUID) -> Review:
        review = await self._lock_review(review_id)
        if review.status not in {ReviewStatus.QUEUED, ReviewStatus.PROCESSING}:
            raise ReviewStateError("Only a queued review can begin processing")
        review.status = ReviewStatus.PROCESSING
        await self.session.flush()
        return review

    async def complete_review(self, review_id: UUID, result: ReviewCompletion) -> Review:
        review = await self._lock_review(review_id)
        if review.status != ReviewStatus.PROCESSING:
            raise ReviewStateError("Only a processing review can be completed")
        self.session.add_all(
            Finding(review_id=review_id, **finding.model_dump()) for finding in result.findings
        )
        review.status = result.status
        review.risk = result.risk
        review.risk_score = result.risk_score
        review.summary = result.summary
        review.recommendation = result.recommendation
        review.completed_at = datetime.now(UTC)
        await self.session.flush()
        return review

    async def fail_review(self, review_id: UUID, error_message: str) -> Review:
        if not error_message.strip() or len(error_message) > 2000:
            raise ValueError("Failure message must contain 1 to 2000 characters")
        review = await self._lock_review(review_id)
        if review.status not in {ReviewStatus.QUEUED, ReviewStatus.PROCESSING}:
            raise ReviewStateError("Only a queued or processing review can fail")
        review.status = ReviewStatus.FAILED
        review.error_message = error_message
        review.completed_at = datetime.now(UTC)
        await self.session.flush()
        return review
