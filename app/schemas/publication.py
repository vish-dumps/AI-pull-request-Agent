from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.github import CommitSHA, RepositoryName
from app.schemas.review import FindingInput, Recommendation, ReviewStatus, Severity


class PublicationStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    PUBLISHED = "PUBLISHED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class PublicationRead(BaseModel):
    review_id: UUID
    status: PublicationStatus | Literal["DISABLED", "NOT_REQUESTED"]
    attempts: int = 0
    post_attempts: int = 0
    comment_id: int | None = None
    error: str | None = None
    next_retry_at: datetime | None = None
    published_at: datetime | None = None


class CommentReview(BaseModel):
    review_id: UUID
    repository: RepositoryName
    pr_number: int = Field(gt=0)
    base_sha: CommitSHA
    head_sha: CommitSHA
    status: ReviewStatus
    risk: Severity | None
    risk_score: float | None
    recommendation: Recommendation | None
    summary: str | None
    findings: list[FindingInput]
    mock_mode: bool | None = None

    @classmethod
    def from_stored(cls, review) -> "CommentReview":
        return cls(
            review_id=review.id,
            repository=review.pull_request.repository.github_full_name,
            pr_number=review.pull_request.pr_number,
            base_sha=review.base_sha,
            head_sha=review.head_sha,
            status=review.status,
            risk=review.risk,
            risk_score=review.risk_score,
            recommendation=review.recommendation,
            summary=review.summary,
            mock_mode=review.mock_mode,
            findings=[
                FindingInput.model_validate(finding, from_attributes=True)
                for finding in review.findings
            ],
        )
