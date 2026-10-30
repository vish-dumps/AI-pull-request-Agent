"""Measure an in-process, mock-only delivery simulation. No external services or writes."""

import argparse
import asyncio
import json
import math
import os
import platform
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config
from fakeredis.aioredis import FakeRedis
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import build_session_factory
from app.main import create_app
from app.schemas.github import PullRequestContext
from app.services.github_service import GitHubService, build_github_client
from app.services.llm_service import MockLLMProvider
from app.services.pr_cache import CachedGitHubService
from app.services.worker_queue import PROCESSING, WorkerQueue
from app.worker.processor import WorkflowProcessor
from app.worker.runner import ReviewWorker

ROOT = Path(__file__).resolve().parents[1]


def percentile_ms(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)] * 1000, 3)


class DelayedMockProvider(MockLLMProvider):
    def __init__(self, delay_ms):
        self.delay = delay_ms / 1000
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return await super().generate(request)


class SimulationProcessor(WorkflowProcessor):
    @asynccontextmanager
    async def lock(self, job):
        # One worker in this harness. Production ownership requires PostgreSQL.
        yield True


async def simulate(requests=100, concurrency=10, model_delay_ms=10, duplicate_every=10):
    import hashlib
    import hmac

    # Class defaults only: developer credentials and feature flags cannot alter this demo.
    settings = Settings.model_construct()
    settings.log_level = "WARNING"
    settings.github_webhook_secret = SecretStr("local-simulation-secret")
    context = PullRequestContext.model_validate_json(
        (ROOT / "examples/multi_agent_pr.json").read_bytes()
    )
    pr = {
        "number": context.pr_number,
        "title": context.title,
        "body": context.description,
        "user": {"login": context.author},
        "base": {
            "ref": context.base_branch,
            "sha": context.base_sha,
            "repo": {"full_name": context.repository},
        },
        "head": {
            "ref": context.head_branch,
            "sha": context.head_sha,
            "repo": {"full_name": context.repository},
        },
        "changed_files": context.total_changed_files,
        "additions": context.total_additions,
        "deletions": context.total_deletions,
    }
    body = json.dumps(
        {
            "action": "synchronize",
            "number": context.pr_number,
            "repository": {"full_name": context.repository},
            "pull_request": pr,
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"local-simulation-secret", body, hashlib.sha256).hexdigest()
    remote_calls = Counter()

    def github_response(request):
        assert request.method == "GET", "Simulation must never publish"
        if request.url.path.endswith("/files"):
            remote_calls["files"] += 1
            return httpx.Response(200, json=[file.model_dump() for file in context.changed_files])
        remote_calls["pr"] += 1
        return httpx.Response(200, json=pr)

    with tempfile.TemporaryDirectory(prefix="pr-agent-load-") as directory:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(directory) / 'reviews.db'}", poolclass=NullPool
        )

        @event.listens_for(engine.sync_engine, "connect")
        def sqlite_connection(connection, record):
            connection.isolation_level = None
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine.sync_engine, "begin")
        def sqlite_begin(connection):
            connection.exec_driver_sql("BEGIN")

        def migrate(connection):
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "alembic"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        try:
            async with engine.begin() as connection:
                await connection.run_sync(migrate)
            sessions = build_session_factory(engine)
            async with FakeRedis(decode_responses=True) as redis:
                app = create_app(settings)

                @asynccontextmanager
                async def resources(application):
                    application.state.settings = settings
                    application.state.db_engine = engine
                    application.state.session_factory = sessions
                    application.state.redis = redis
                    yield

                app.router.lifespan_context = resources
                async with resources(app):
                    async with build_github_client(
                        settings, httpx.MockTransport(github_response)
                    ) as gh:
                        saver = InMemorySaver()

                        @asynccontextmanager
                        async def checkpoints():
                            yield saver

                        provider = DelayedMockProvider(model_delay_ms)
                        processor = SimulationProcessor(
                            engine,
                            sessions,
                            settings,
                            provider,
                            CachedGitHubService(GitHubService(gh, settings), redis, settings),
                            checkpointers=checkpoints,
                        )
                        queue = WorkerQueue(redis, settings)
                        worker = ReviewWorker(queue, processor, settings)
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app), base_url="http://simulation.invalid"
                        ) as client:
                            return await measure(
                                client,
                                redis,
                                worker,
                                provider,
                                remote_calls,
                                body,
                                signature,
                                requests,
                                concurrency,
                                duplicate_every,
                                model_delay_ms,
                            )
        finally:
            await engine.dispose()


async def measure(
    client,
    redis,
    worker,
    provider,
    remote_calls,
    body,
    signature,
    requests,
    concurrency,
    duplicate_every,
    model_delay_ms,
):
    latencies, reads, review_ids, failures = [], [], set(), []
    duplicates = 0
    produced = asyncio.Event()
    semaphore = asyncio.Semaphore(concurrency)
    deliveries = []
    for index in range(requests):
        duplicate = duplicate_every > 0 and index > 0 and (index + 1) % duplicate_every == 0
        deliveries.append(deliveries[-1] if duplicate else str(uuid4()))
    observed_depth = 0
    processed = 0
    started = perf_counter()

    async def send(delivery):
        nonlocal duplicates, observed_depth
        async with semaphore:
            before = perf_counter()
            response = await client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": delivery,
                    "X-Hub-Signature-256": signature,
                },
            )
            latencies.append(perf_counter() - before)
            if response.status_code != 202:
                failures.append(response.status_code)
                return
            accepted = response.json()
            review_ids.add(accepted["review_id"])
            duplicates += accepted["duplicate"]
            observed_depth = max(observed_depth, await redis.llen("reviews:pending"))

    async def consume():
        nonlocal processed
        while True:
            if await worker.run_once():
                processed += 1
            elif produced.is_set():
                return
            else:
                await asyncio.sleep(0.001)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.gather(*(send(delivery) for delivery in deliveries))
        acceptance_seconds = perf_counter() - started
        produced.set()
        await consumer
        processing_seconds = perf_counter() - started
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    completed = partial = failed = 0
    for review_id in sorted(review_ids):
        before = perf_counter()
        response = await client.get(f"/reviews/{review_id}")
        reads.append(perf_counter() - before)
        if response.status_code != 200:
            failed += 1
            continue
        result = response.json()
        completed += result["status"] == "COMPLETED"
        partial += result["status"] == "PARTIAL"
        failed += result["status"] not in {"COMPLETED", "PARTIAL"}
        status = await WorkerQueue(redis, worker.settings).status(UUID(review_id))
        if status is None or status.status != result["status"]:
            failures.append("status_mismatch")
    remaining = await redis.llen("reviews:pending")
    inflight = await redis.hlen(PROCESSING)
    return {
        "mode": "in_process_simulation",
        "recorded_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "boundaries": {
            "http": "ASGI in process",
            "database": "temporary migrated SQLite",
            "redis": "fakeredis with Lua",
            "checkpoints": "in memory",
            "workers": 1,
            "review_lock": "single-worker substitute",
            "github": "fixture HTTP transport",
            "model": "delayed deterministic mock",
            "publishing": False,
            "retrieval": "diff_only",
        },
        "input": {
            "requests": requests,
            "concurrency": concurrency,
            "model_delay_ms": model_delay_ms,
            "duplicate_every": duplicate_every,
            "same_pr_and_commit": True,
        },
        "outcome": {
            "accepted": requests - sum(isinstance(value, int) for value in failures),
            "duplicates": duplicates,
            "unique_reviews": len(review_ids),
            "completed": completed,
            "partial": partial,
            "failed": failed,
            "errors": failures,
            "processed_receipts": processed,
            "model_calls": provider.calls,
            "github_pr_reads": remote_calls["pr"],
            "github_file_fetches": remote_calls["files"],
            "remaining_queue": remaining,
            "inflight": inflight,
        },
        "timings": {
            "acceptance_seconds": round(acceptance_seconds, 4),
            "drain_seconds_from_start": round(processing_seconds, 4),
            "accepted_requests_per_second": round(
                (requests - sum(isinstance(value, int) for value in failures)) / acceptance_seconds,
                2,
            ),
            "completed_reviews_per_second": round(completed / processing_seconds, 2),
            "acceptance_ms": {
                "p50": percentile_ms(latencies, 0.5),
                "p95": percentile_ms(latencies, 0.95),
                "p99": percentile_ms(latencies, 0.99),
            },
            "review_read_ms": {"p50": percentile_ms(reads, 0.5), "p95": percentile_ms(reads, 0.95)},
            "observed_max_pending_queue": observed_depth,
        },
        "successful": not failures
        and not failed
        and not partial
        and completed == len(review_ids)
        and not remaining
        and not inflight,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--model-delay-ms", type=float, default=10)
    parser.add_argument(
        "--duplicate-every", type=int, default=10, help="0 disables duplicate deliveries"
    )
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        not 1 <= args.requests <= 1000
        or not 1 <= args.concurrency <= 100
        or not 0 <= args.model_delay_ms <= 1000
        or args.duplicate_every < 0
        or not math.isfinite(args.timeout_seconds)
        or not 0 < args.timeout_seconds <= 600
    ):
        parser.error(
            "Use requests 1..1000, concurrency 1..100, delay 0..1000 ms, "
            "duplicate interval >=0, timeout (0,600] seconds"
        )
    configure_logging("WARNING")
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

    async def run():
        async with asyncio.timeout(args.timeout_seconds):
            return await simulate(
                args.requests, args.concurrency, args.model_delay_ms, args.duplicate_every
            )

    try:
        result = asyncio.run(run())
    except TimeoutError:
        parser.exit(1, "Simulation exceeded its deadline; no successful benchmark was recorded.\n")
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    raise SystemExit(0 if result["successful"] else 1)


if __name__ == "__main__":
    main()
