import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])
DependencyStatus = Literal["up", "down"]


class Dependencies(BaseModel):
    postgres: DependencyStatus
    pgvector: DependencyStatus
    redis: DependencyStatus


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    dependencies: Dependencies


async def check_postgres(
    engine: AsyncEngine, timeout_seconds: float
) -> tuple[DependencyStatus, DependencyStatus]:
    try:
        async with asyncio.timeout(timeout_seconds), engine.connect() as connection:
            result = await connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            )
        return "up", "up" if result else "down"
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        logger.warning(
            "Dependency health check failed",
            extra={"dependency": "postgres", "error_type": type(error).__name__},
        )
        return "down", "down"


async def check_redis(client: Redis, timeout_seconds: float) -> DependencyStatus:
    try:
        async with asyncio.timeout(timeout_seconds):
            return "up" if await client.ping() else "down"
    except (RedisError, OSError, TimeoutError) as error:
        logger.warning(
            "Dependency health check failed",
            extra={"dependency": "redis", "error_type": type(error).__name__},
        )
        return "down"


@router.get("/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
async def health(request: Request, response: Response) -> HealthResponse:
    timeout = request.app.state.settings.health_check_timeout_seconds
    (postgres, pgvector), redis = await asyncio.gather(
        check_postgres(request.app.state.db_engine, timeout),
        check_redis(request.app.state.redis, timeout),
    )
    healthy = postgres == pgvector == redis == "up"
    response.status_code = 200 if healthy else 503
    return HealthResponse(
        status="healthy" if healthy else "degraded",
        dependencies=Dependencies(postgres=postgres, pgvector=pgvector, redis=redis),
    )
