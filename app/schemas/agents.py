from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.review import AgentType, FindingInput, ReviewStatus


class SecurityFinding(FindingInput):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    # Nullable but required: strict JSON schemas require every property to be present.
    line_number: int | None = Field(gt=0)
    evidence: str = Field(min_length=1, max_length=500)

    @field_validator("evidence")
    @classmethod
    def require_visible_evidence(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Evidence must contain a code fragment")
        return value

    @field_validator("agent_type")
    @classmethod
    def require_security_category(cls, value: AgentType) -> AgentType:
        if value != AgentType.SECURITY:
            raise ValueError("Security findings must use the security agent type")
        return value


class SecurityReview(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    findings: list[SecurityFinding] = Field(max_length=10)
    summary: str = Field(min_length=1, max_length=2000)
    risk_score: int = Field(ge=0, le=100, strict=True)


class SecurityAgentResult(BaseModel):
    agent: Literal[AgentType.SECURITY] = AgentType.SECURITY
    status: Literal["COMPLETED", "FAILED"]
    review: SecurityReview | None = None
    error: str | None = None
    attempts: int = Field(ge=0, le=2)
    duration_ms: float = Field(ge=0)
    coverage_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_outcome(self) -> "SecurityAgentResult":
        if self.status == "COMPLETED" and (self.review is None or self.error is not None):
            raise ValueError("Completed agent results require a review and no error")
        if self.status == "FAILED" and (self.review is not None or not self.error):
            raise ValueError("Failed agent results require an error and no review")
        return self


class SecurityRunResult(BaseModel):
    review_id: UUID
    status: ReviewStatus
    reused: bool = False
    agent_result: SecurityAgentResult | None = None
