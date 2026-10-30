from operator import add
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.schemas.agents import AgentResult
from app.schemas.context import RetrievedChunk
from app.schemas.review import ReviewCompletion, ReviewStatus


def merge_agent_results(
    previous: dict[str, dict[str, Any]], updates: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {**previous, **updates}


class WorkflowError(TypedDict):
    stage: str
    message: str


class ReviewState(TypedDict, total=False):
    # Only JSON primitives enter checkpoints, never clients, sessions, or secrets.
    schema_version: int
    review_id: str
    job: dict[str, Any]
    pr_context: dict[str, Any]
    review_context: dict[str, Any]
    mock_mode: bool
    persistence_enabled: bool
    retrieved_context: list[dict[str, Any]]
    context_source: str
    retrieval_identity: str | None
    expected_agents: list[str]
    agent_results: Annotated[dict[str, dict[str, Any]], merge_agent_results]
    security_result: dict[str, Any]
    aggregated_review: dict[str, Any] | None
    errors: Annotated[list[WorkflowError], add]
    completed_stages: Annotated[list[str], add]
    status: str
    persisted: bool  # Final application result, distinct from checkpoint durability.
    reused: bool


class WorkflowReport(BaseModel):
    review_id: UUID
    status: ReviewStatus
    mock_mode: bool
    persisted: bool = Field(description="Whether the final application review outcome is persisted")
    reused: bool = False
    paused: bool
    completed_stages: list[str]
    pending_nodes: list[str]
    errors: list[WorkflowError] = Field(default_factory=list)
    review: ReviewCompletion | None = None
    agent_results: dict[str, AgentResult] = Field(default_factory=dict)
    context_source: str = "diff_only"
    retrieved_context: list[RetrievedChunk] = Field(default_factory=list)
    context_warnings: list[str] = Field(default_factory=list)
