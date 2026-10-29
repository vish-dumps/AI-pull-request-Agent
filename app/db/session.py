import logging
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

logger = logging.getLogger(__name__)


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=settings.health_check_timeout_seconds,
        connect_args={"timeout": settings.health_check_timeout_seconds},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Provide a request-scoped read session; writes use explicit transactions."""
    try:
        async with request.app.state.session_factory() as session:
            yield session
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        logger.warning(
            "Review database request failed",
            extra={"stage": "database", "error_type": type(error).__name__},
        )
        raise HTTPException(503, "Review database unavailable") from None
