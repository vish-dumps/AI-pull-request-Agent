import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.schemas.jobs import JobStatusRead
from app.services.worker_queue import WorkerQueue

router = APIRouter(tags=["reviews"])


@router.get("/reviews/{review_id}/status", response_model=JobStatusRead)
async def get_job_status(review_id: UUID, request: Request) -> JobStatusRead:
    settings = request.app.state.settings
    try:
        async with asyncio.timeout(settings.redis_operation_timeout_seconds):
            result = await WorkerQueue(request.app.state.redis, settings).status(review_id)
    except (RedisError, OSError, TimeoutError, ValidationError, ValueError):
        raise HTTPException(503, "Review job status is unavailable") from None
    if result is None:
        raise HTTPException(404, "Review job status is unknown or expired")
    return result
