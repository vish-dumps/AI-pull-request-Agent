from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CommentUnavailableError
from app.db.models import ReviewPublication
from app.db.repositories import ReviewRepository
from app.db.session import get_session
from app.schemas.publication import CommentReview, PublicationRead
from app.schemas.review import ReviewStatus
from app.services.comment_renderer import render_comment
from app.services.publication_service import publication_read

router = APIRouter(tags=["reviews"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/reviews/{review_id}/comment", response_class=PlainTextResponse)
async def preview_comment(review_id: UUID, request: Request, session: Session) -> PlainTextResponse:
    review = await ReviewRepository(session).get_review(review_id)
    if review is None:
        raise HTTPException(404, "Review not found")
    publication = await session.get(ReviewPublication, review_id)
    try:
        body = (
            publication.body
            if publication is not None and publication.body is not None
            else render_comment(CommentReview.from_stored(review), request.app.state.settings)
        )
    except CommentUnavailableError:
        raise HTTPException(
            409, "Only completed or partial reviews have a comment preview"
        ) from None
    return PlainTextResponse(body, media_type="text/markdown")


@router.get("/reviews/{review_id}/publication", response_model=PublicationRead)
async def get_publication(review_id: UUID, request: Request, session: Session) -> PublicationRead:
    review = await ReviewRepository(session).get_review(review_id)
    if review is None:
        raise HTTPException(404, "Review not found")
    publication = await session.get(ReviewPublication, review_id)
    if publication is not None:
        return publication_read(publication)
    if review.status == ReviewStatus.FAILED:
        return PublicationRead(
            review_id=review_id, status="SKIPPED", error="Failed analysis has no review comment"
        )
    return PublicationRead(
        review_id=review_id,
        status="NOT_REQUESTED" if request.app.state.settings.github_publish_enabled else "DISABLED",
    )
