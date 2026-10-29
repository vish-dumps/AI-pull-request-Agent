from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class ReviewStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Recommendation(StrEnum):
    APPROVE = "APPROVE"
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class AgentType(StrEnum):
    SECURITY = "security"
    QUALITY = "quality"
    TESTING = "testing"
    DOCUMENTATION = "documentation"


class FindingInput(BaseModel):
    agent_type: AgentType
    file_path: str = Field(min_length=1)
    line_number: int | None = Field(default=None, gt=0)
    severity: Severity
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class ReviewCompletion(BaseModel):
    status: Literal[ReviewStatus.COMPLETED, ReviewStatus.PARTIAL] = ReviewStatus.COMPLETED
    risk: Severity
    risk_score: Annotated[FiniteFloat, Field(ge=0)]
    summary: str = Field(min_length=1)
    recommendation: Recommendation
    findings: list[FindingInput] = Field(default_factory=list)


class FindingRead(FindingInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    review_id: UUID


class ReviewSummary(BaseModel):
    id: UUID
    status: ReviewStatus
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    risk: Severity | None
    risk_score: float | None
    summary: str | None
    recommendation: Recommendation | None
    created_at: datetime
    completed_at: datetime | None


class ReviewRead(ReviewSummary):
    findings: list[FindingRead]
