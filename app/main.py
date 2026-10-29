import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.routes.health import router as health_router
from app.api.routes.reviews import router as reviews_router
from app.api.routes.webhooks import router as webhooks_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import build_engine, build_session_factory

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        async with AsyncExitStack() as resources:
            engine = build_engine(settings)
            resources.push_async_callback(engine.dispose)
            redis = Redis.from_url(
                str(settings.redis_url),
                decode_responses=True,
                socket_connect_timeout=settings.health_check_timeout_seconds,
                socket_timeout=settings.health_check_timeout_seconds,
            )
            resources.push_async_callback(redis.aclose)
            application.state.settings = settings
            application.state.db_engine = engine
            application.state.session_factory = build_session_factory(engine)
            application.state.redis = redis
            logger.info("Application started")
            yield
        logger.info("Application stopped")

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="AI pull request review agent. Phase 4: structured security reviews.",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    application.include_router(reviews_router)
    application.include_router(webhooks_router)
    return application


app = create_app()
