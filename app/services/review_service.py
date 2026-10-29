from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.security_agent import SecurityAgent
from app.graph.workflow import ReviewWorkflow
from app.schemas.agents import SecurityAgentResult, SecurityRunResult
from app.schemas.github import PullRequestContext
from app.schemas.jobs import ReviewJob
from app.services.aggregation_service import security_completion as security_completion


class SecurityReviewService:
    """Phase 4 interface, now delegating to the shared five-stage graph."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], agent: SecurityAgent, *, mock_mode: bool
    ) -> None:
        self.workflow = ReviewWorkflow(agent, sessions=sessions, mock_mode=mock_mode)

    async def run(self, job: ReviewJob, context: PullRequestContext) -> SecurityRunResult:
        state = await self.workflow.start(job, context)
        return SecurityRunResult(
            review_id=job.review_id,
            status=state["status"],
            reused=state["reused"],
            agent_result=(
                SecurityAgentResult.model_validate(state["security_result"])
                if state.get("security_result") and not state["reused"]
                else None
            ),
        )
