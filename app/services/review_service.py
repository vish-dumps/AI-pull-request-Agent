from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.security_agent import SecurityAgent
from app.db.repositories import ReviewRepository
from app.schemas.agents import SecurityAgentResult, SecurityRunResult
from app.schemas.github import PullRequestContext
from app.schemas.jobs import ReviewJob
from app.schemas.review import (
    FindingInput,
    Recommendation,
    ReviewCompletion,
    ReviewStatus,
    Severity,
)

SEVERITY_WEIGHTS = {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 5, Severity.CRITICAL: 10}


def security_completion(result: SecurityAgentResult, *, mock_mode: bool) -> ReviewCompletion:
    if result.review is None or result.status != "COMPLETED":
        raise ValueError("Cannot complete a review with a failed security result")
    findings = [
        FindingInput.model_validate(finding.model_dump(exclude={"evidence"}))
        for finding in result.review.findings
    ]
    score = round(sum(SEVERITY_WEIGHTS[f.severity] * f.confidence for f in findings), 2)
    risk = (
        Severity.CRITICAL
        if score >= 8
        else (Severity.HIGH if score >= 4 else Severity.MEDIUM if score >= 1 else Severity.LOW)
    )
    # Other reviewer categories have not run, so even zero findings is a partial review.
    return ReviewCompletion(
        status=ReviewStatus.PARTIAL,
        risk=risk,
        risk_score=score,
        recommendation=Recommendation.REQUEST_CHANGES if score >= 4 else Recommendation.COMMENT,
        summary=("[MOCK] " if mock_mode else "")
        + "Security-only partial review. "
        + result.review.summary
        + " Testing, quality, and documentation agents were not run."
        + (" Coverage was limited." if result.coverage_warnings else ""),
        findings=findings,
    )


class SecurityReviewService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], agent: SecurityAgent, *, mock_mode: bool
    ) -> None:
        self.sessions = sessions
        self.agent = agent
        self.mock_mode = mock_mode

    async def run(self, job: ReviewJob, context: PullRequestContext) -> SecurityRunResult:
        async with self.sessions.begin() as session:
            store = ReviewRepository(session)
            review = await store.create_review(job, context)
            if review.status in {ReviewStatus.COMPLETED, ReviewStatus.PARTIAL, ReviewStatus.FAILED}:
                return SecurityRunResult(review_id=review.id, status=review.status, reused=True)
            await store.mark_processing(review.id)
        # External model I/O never holds a database transaction open.
        result = await self.agent.review(context)
        async with self.sessions.begin() as session:
            store = ReviewRepository(session)
            if result.status == "FAILED":
                review = await store.fail_review(
                    job.review_id, result.error or "Security agent failed"
                )
            else:
                review = await store.complete_review(
                    job.review_id, security_completion(result, mock_mode=self.mock_mode)
                )
            status = review.status
        return SecurityRunResult(review_id=job.review_id, status=status, agent_result=result)
