from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Review
from app.db.repositories import ReviewRepository
from app.db.session import get_session
from app.schemas.github import validate_repository
from app.schemas.review import FindingRead, ReviewRead, ReviewSummary

router = APIRouter(tags=["reviews"])
Session = Annotated[AsyncSession, Depends(get_session)]


def review_summary(review: Review) -> ReviewSummary:
    return ReviewSummary(
        id=review.id,
        status=review.status,
        repository=review.pull_request.repository.github_full_name,
        pr_number=review.pull_request.pr_number,
        base_sha=review.base_sha,
        head_sha=review.head_sha,
        risk=review.risk,
        risk_score=review.risk_score,
        summary=review.summary,
        recommendation=review.recommendation,
        created_at=review.created_at,
        completed_at=review.completed_at,
    )


async def require_review(session: AsyncSession, review_id: UUID) -> Review:
    review = await ReviewRepository(session).get_review(review_id)
    if review is None:
        raise HTTPException(404, "Review not found")
    return review


@router.get("/reviews/{review_id}", response_model=ReviewRead)
async def get_review(review_id: UUID, session: Session) -> ReviewRead:
    review = await require_review(session, review_id)
    return ReviewRead(
        **review_summary(review).model_dump(),
        findings=[FindingRead.model_validate(finding) for finding in review.findings],
    )


@router.get("/reviews/{review_id}/findings", response_model=list[FindingRead])
async def get_findings(review_id: UUID, session: Session) -> list[FindingRead]:
    review = await require_review(session, review_id)
    return [FindingRead.model_validate(finding) for finding in review.findings]


@router.get("/repositories/{owner}/{repository}/reviews", response_model=list[ReviewSummary])
async def list_repository_reviews(
    owner: str,
    repository: str,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReviewSummary]:
    try:
        name = validate_repository(f"{owner}/{repository}")
    except ValueError:
        raise HTTPException(422, "Invalid repository identifier") from None
    reviews = await ReviewRepository(session).list_repository_reviews(
        name, limit=limit, offset=offset
    )
    return [review_summary(review) for review in reviews]
