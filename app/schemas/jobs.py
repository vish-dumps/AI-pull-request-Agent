from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.schemas.github import CommitSHA, PRNumber, RepositoryName

PullRequestAction = Literal["opened", "synchronize", "reopened"]
SUPPORTED_PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


class ReviewJob(BaseModel):
    review_id: UUID = Field(default_factory=uuid4)
    delivery_id: UUID
    repository: RepositoryName
    pr_number: PRNumber
    action: PullRequestAction
    base_sha: CommitSHA
    head_sha: CommitSHA
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WebhookAccepted(BaseModel):
    review_id: UUID
    status: Literal["QUEUED"] = "QUEUED"
    duplicate: bool = False


class WebhookIgnored(BaseModel):
    status: Literal["ignored"] = "ignored"
    reason: str
