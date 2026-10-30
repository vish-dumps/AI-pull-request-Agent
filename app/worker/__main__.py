import argparse
import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack

from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import build_engine, build_session_factory
from app.services.context_service import ContextService
from app.services.embedding_service import build_embedding_provider
from app.services.github_service import GitHubService, build_github_client
from app.services.llm_service import build_llm_provider
from app.services.pr_cache import CachedGitHubService
from app.services.worker_queue import WorkerQueue
from app.worker.processor import WorkflowProcessor
from app.worker.runner import ReviewWorker

logger = logging.getLogger(__name__)


async def run(settings: Settings, *, once: bool) -> None:
    configure_logging(settings.log_level)
    async with AsyncExitStack() as resources:
        engine = build_engine(settings)
        resources.push_async_callback(engine.dispose)
        redis = Redis.from_url(
            str(settings.redis_url),
            decode_responses=False,
            socket_connect_timeout=settings.redis_operation_timeout_seconds,
            socket_timeout=settings.redis_operation_timeout_seconds,
        )
        resources.push_async_callback(redis.aclose)
        provider = await resources.enter_async_context(build_llm_provider(settings))
        github_client = await resources.enter_async_context(build_github_client(settings))
        github = CachedGitHubService(GitHubService(github_client, settings), redis, settings)
        sessions = build_session_factory(engine)
        context = None
        if settings.retrieval_enabled:
            embeddings = await resources.enter_async_context(build_embedding_provider(settings))
            context = ContextService(sessions, embeddings, settings)
        processor = WorkflowProcessor(engine, sessions, settings, provider, github, context)
        worker = ReviewWorker(WorkerQueue(redis, settings), processor, settings)
        if once:
            await worker.run_once()
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda signum, frame: loop.call_soon_threadsafe(stop.set))
        try:
            logger.info("Review worker started", extra={"stage": "worker"})
            await worker.run_forever(stop)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            logger.info("Review worker stopped", extra={"stage": "worker"})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consume queued PR reviews; never publish GitHub comments."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll/maintain once, process at most one receipt, then exit",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force free reviewer and embedding mocks; GitHub reads remain real",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--with-context", action="store_true")
    mode.add_argument("--diff-only", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if args.mock:
        settings = settings.model_copy(update={"llm_mock_mode": True, "embedding_mock_mode": True})
    if args.with_context or args.diff_only:
        settings = settings.model_copy(update={"retrieval_enabled": args.with_context})
    try:
        with asyncio.Runner(
            loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None
        ) as runner:
            runner.run(run(settings, once=args.once))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        logger.error(
            "Worker stopped after an operational error",
            extra={"stage": "worker", "error_type": type(error).__name__},
        )
        parser.exit(
            1, "Worker could not run; check configuration, migrations, Redis, and PostgreSQL.\n"
        )


if __name__ == "__main__":
    main()
