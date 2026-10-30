import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import (
    EmbeddingError,
    GitHubAPIError,
    GitHubRateLimitError,
    RepositoryIndexError,
)
from app.schemas.context import IndexReport, IndexRequest
from app.services.embedding_service import build_embedding_provider
from app.services.github_service import GitHubService, build_github_client
from app.services.indexing_service import IndexingService

router = APIRouter(tags=["repositories"])
logger = logging.getLogger(__name__)


async def indexing_services(
    request: Request,
) -> AsyncIterator[tuple[IndexingService, GitHubService]]:
    settings = request.app.state.settings
    async with AsyncExitStack() as resources:
        try:
            provider = await resources.enter_async_context(build_embedding_provider(settings))
        except EmbeddingError:
            raise HTTPException(503, "Embedding provider is not configured") from None
        client = await resources.enter_async_context(build_github_client(settings))
        yield (
            IndexingService(request.app.state.session_factory, provider, settings),
            GitHubService(client, settings),
        )


@router.post("/repositories/index", response_model=IndexReport)
async def index_repository(
    body: IndexRequest,
    services: Annotated[tuple[IndexingService, GitHubService], Depends(indexing_services)],
) -> IndexReport:
    started = perf_counter()
    indexer, github = services
    try:
        result = await indexer.index_github(github, body.repository, body.ref)
    except GitHubRateLimitError:
        raise HTTPException(503, "GitHub rate limit reached; retry later") from None
    except GitHubAPIError as error:
        raise HTTPException(
            404 if error.status_code == 404 else 502, "Repository source could not be fetched"
        ) from None
    except EmbeddingError:
        raise HTTPException(502, "Repository embeddings could not be generated") from None
    except (SQLAlchemyError, RepositoryIndexError):
        raise HTTPException(503, "Repository index storage is unavailable") from None
    except TimeoutError:
        raise HTTPException(504, "Repository indexing exceeded its deadline") from None
    logger.info(
        "Repository indexing completed",
        extra={
            "repository": result.repository,
            "stage": "index",
            "status": "PARTIAL" if result.limited else "COMPLETED",
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        },
    )
    return result
