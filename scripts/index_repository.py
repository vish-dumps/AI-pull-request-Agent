"""Index a GitHub revision or normalized fixture into PostgreSQL/PGVector."""

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.exceptions import EmbeddingError, GitHubAPIError, RepositoryIndexError
from app.db.session import build_engine, build_session_factory
from app.services.embedding_service import build_embedding_provider
from app.services.github_service import GitHubService, build_github_client
from app.services.indexing_service import IndexingService


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.mock:
        settings = settings.model_copy(update={"embedding_mock_mode": True})
    async with AsyncExitStack() as resources:
        engine = build_engine(settings)
        resources.push_async_callback(engine.dispose)
        provider = await resources.enter_async_context(build_embedding_provider(settings))
        indexer = IndexingService(build_session_factory(engine), provider, settings)
        if args.fixture:
            data = json.loads(args.fixture.read_text(encoding="utf-8-sig"))
            if not isinstance(data["files"], dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in data["files"].items()
            ):
                raise ValueError("Fixture files must map repository paths to source strings")
            result = await indexer.index_files(
                data["repository"], data["commit_sha"], data["files"]
            )
        else:
            client = await resources.enter_async_context(build_github_client(settings))
            result = await indexer.index_github(
                GitHubService(client, settings), args.repository, args.ref
            )
        print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", nargs="?")
    parser.add_argument(
        "--ref", help="Branch, tag, or commit; defaults to repository default branch"
    )
    parser.add_argument("--fixture", type=Path, help="Normalized source fixture; no GitHub calls")
    parser.add_argument(
        "--mock", action="store_true", help="Force free lexical demonstration embeddings"
    )
    args = parser.parse_args()
    if bool(args.repository) == bool(args.fixture) or (args.fixture and args.ref):
        parser.error("Supply either OWNER/REPO [--ref REF] or --fixture PATH")
    try:
        asyncio.run(run(args))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ValidationError,
        SQLAlchemyError,
        EmbeddingError,
        GitHubAPIError,
        RepositoryIndexError,
        TimeoutError,
    ):
        parser.exit(
            1,
            "Indexing failed: check source, embeddings configuration, migrations, and database.\n",
        )


if __name__ == "__main__":
    main()
