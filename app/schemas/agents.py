from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.review import AgentType, FindingInput, ReviewStatus


class EvidenceFinding(FindingInput):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_agent: ClassVar[AgentType | None] = None

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
    def require_category(cls, value: AgentType) -> AgentType:
        if cls.expected_agent is not None and value != cls.expected_agent:
            raise ValueError("Finding category must match its reviewer")
        return value


class SecurityFinding(EvidenceFinding):
    expected_agent = AgentType.SECURITY


class QualityFinding(EvidenceFinding):
    expected_agent = AgentType.QUALITY


class TestingFinding(EvidenceFinding):
    expected_agent = AgentType.TESTING


class DocumentationFinding(EvidenceFinding):
    expected_agent = AgentType.DOCUMENTATION


class AgentReview(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    findings: list[EvidenceFinding] = Field(max_length=10)
    summary: str = Field(min_length=1, max_length=2000)
    risk_score: int = Field(ge=0, le=100, strict=True)


class SecurityReview(AgentReview):
    findings: list[SecurityFinding] = Field(max_length=10)


class QualityReview(AgentReview):
    findings: list[QualityFinding] = Field(max_length=10)


class TestingReview(AgentReview):
    findings: list[TestingFinding] = Field(max_length=10)


class DocumentationReview(AgentReview):
    findings: list[DocumentationFinding] = Field(max_length=10)


class AgentResult(BaseModel):
    agent: AgentType
    status: Literal["COMPLETED", "FAILED"]
    review: AgentReview | None = None
    error: str | None = None
    attempts: int = Field(ge=0, le=2)
    duration_ms: float = Field(ge=0)
    coverage_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_outcome(self) -> "AgentResult":
        if self.status == "COMPLETED" and (self.review is None or self.error is not None):
            raise ValueError("Completed agent results require a review and no error")
        if self.status == "FAILED" and (self.review is not None or not self.error):
            raise ValueError("Failed agent results require an error and no review")
        if self.review and any(
            finding.agent_type != self.agent for finding in self.review.findings
        ):
            raise ValueError("Findings must match their result's agent type")
        return self


class SecurityAgentResult(AgentResult):
    agent: Literal[AgentType.SECURITY] = AgentType.SECURITY
    review: SecurityReview | None = None


class QualityAgentResult(AgentResult):
    agent: Literal[AgentType.QUALITY] = AgentType.QUALITY
    review: QualityReview | None = None


class TestingAgentResult(AgentResult):
    agent: Literal[AgentType.TESTING] = AgentType.TESTING
    review: TestingReview | None = None


class DocumentationAgentResult(AgentResult):
    agent: Literal[AgentType.DOCUMENTATION] = AgentType.DOCUMENTATION
    review: DocumentationReview | None = None


class SecurityRunResult(BaseModel):
    review_id: UUID
    status: ReviewStatus
    reused: bool = False
    agent_result: SecurityAgentResult | None = None
