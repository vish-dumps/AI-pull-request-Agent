import asyncio
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis
import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PostgresDsn
from sqlalchemy import func, select, text

from app.agents.factory import create_reviewers
from app.core.exceptions import LLMProviderError, PullRequestChangedError, WorkflowPersistenceError
from app.db.models import Finding
from app.db.repositories import ReviewRepository
from app.graph.checkpoints import open_postgres_checkpointer
from app.graph.nodes import ReviewNodes
from app.graph.workflow import ReviewWorkflow
from app.main import create_app
from app.services.github_service import GitHubService, build_github_client
from app.services.job_queue import ReviewJobQueue
from app.services.llm_service import MockLLMProvider
from app.services.pr_cache import CachedGitHubService
from app.services.worker_queue import DEAD, DELAYED, LEASES, PROCESSING, WorkerQueue
from app.worker.locks import review_lock
from app.worker.processor import WorkflowProcessor
from app.worker.runner import ReviewWorker
from tests.support.github import pr_payload, webhook_headers


def github_transport(context, calls):
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[file.model_dump() for file in context.changed_files])
        return httpx.Response(200, json=pr_payload(context))

    return httpx.MockTransport(handler)


class SQLiteTestProcessor(WorkflowProcessor):
    @asynccontextmanager
    async def lock(self, job):
        yield True  # SQLite only tests business I/O; real ownership has separate PG tests.


def make_processor(database, settings, provider, github, saver):
    @asynccontextmanager
    async def checkpointers():
        yield saver

    processor = WorkflowProcessor if database.backend == "postgresql" else SQLiteTestProcessor
    return processor(
        database.engine, database.sessions, settings, provider, github, checkpointers=checkpointers
    )


def test_signed_webhook_runs_to_persisted_review_and_status_api(
    database, settings, resources, monkeypatch, multi_context
):
    server = fakeredis.FakeServer()
    app_redis = FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr("app.main.Redis.from_url", lambda *args, **kwargs: app_redis)
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.session_factory = database.sessions
        body = json.dumps(
            {
                "action": "opened",
                "number": multi_context.pr_number,
                "repository": {"full_name": multi_context.repository},
                "pull_request": pr_payload(multi_context),
            }
        ).encode()
        delivery = str(uuid4())
        headers = webhook_headers(body, delivery=delivery)
        accepted = client.post("/webhooks/github", content=body, headers=headers)
        assert accepted.status_code == 202
        review_id = accepted.json()["review_id"]
        assert client.get(f"/reviews/{review_id}/status").json()["status"] == "QUEUED"

        async def run():
            calls = []
            async with FakeRedis(server=server, decode_responses=True) as redis:
                async with build_github_client(
                    settings, github_transport(multi_context, calls)
                ) as gh_client:
                    provider = MockLLMProvider()
                    provider.generate = AsyncMock(wraps=provider.generate)
                    processor = make_processor(
                        database,
                        settings,
                        provider,
                        CachedGitHubService(GitHubService(gh_client, settings), redis, settings),
                        InMemorySaver(),
                    )
                    worker = ReviewWorker(WorkerQueue(redis, settings), processor, settings)
                    assert await worker.run_once()
                    assert provider.generate.await_count == 4
                    assert await redis.hlen(PROCESSING) == 0
                    assert any(path.endswith("/files") for path in calls)

        asyncio.run(run())
        status = client.get(f"/reviews/{review_id}/status")
        assert status.status_code == 200 and status.json()["status"] == "COMPLETED"
        assert status.json()["attempts"] == 1
        stored = client.get(f"/reviews/{review_id}")
        assert stored.status_code == 200 and len(stored.json()["findings"]) == 4
        repeat = client.post("/webhooks/github", content=body, headers=headers)
        assert repeat.json()["duplicate"] and repeat.json()["status"] == "COMPLETED"


def test_worker_resumes_after_commit_before_checkpoint_without_new_models(
    database, settings, multi_job, multi_context, monkeypatch
):
    original = ReviewNodes.persist
    count = 0

    async def crash(self, state):
        nonlocal count
        update = await original(self, state)
        count += 1
        if count == 1:
            raise WorkflowPersistenceError("Simulated commit/checkpoint gap")
        return update

    monkeypatch.setattr(ReviewNodes, "persist", crash)

    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            async with build_github_client(settings, github_transport(multi_context, [])) as client:
                provider = MockLLMProvider()
                provider.generate = AsyncMock(wraps=provider.generate)
                processor = make_processor(
                    database,
                    settings,
                    provider,
                    CachedGitHubService(GitHubService(client, settings), redis, settings),
                    InMemorySaver(),
                )
                queue = WorkerQueue(redis, settings)
                worker = ReviewWorker(queue, processor, settings)
                await ReviewJobQueue(redis, 60).enqueue(multi_job)
                await worker.run_once()
                assert (await queue.status(multi_job.review_id)).status == "QUEUED"
                tokens = await redis.zrange(DELAYED, 0, -1)
                await redis.zadd(DELAYED, {token: 0 for token in tokens})
                await worker.run_once()
                assert (await queue.status(multi_job.review_id)).status == "COMPLETED"
                assert provider.generate.await_count == 4
                async with database.sessions() as session:
                    assert await session.scalar(select(func.count()).select_from(Finding)) == 4

    asyncio.run(run())


def test_permanent_ingestion_failure_ends_checkpoint_without_fake_review(
    database, settings, multi_job
):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            github = AsyncMock()
            github.get_pull_request_context.side_effect = PullRequestChangedError("private-details")
            saver = InMemorySaver()
            provider = MockLLMProvider()
            provider.generate = AsyncMock(wraps=provider.generate)
            processor = make_processor(database, settings, provider, github, saver)
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(multi_job)
            await ReviewWorker(queue, processor, settings).run_once()
            assert (await queue.status(multi_job.review_id)).status == "FAILED"
            snapshot = await processor.workflow(saver).graph.aget_state(
                processor.workflow(saver).config(multi_job.review_id)
            )
            assert snapshot.values["status"] == "FAILED" and not snapshot.next
            assert await redis.llen(DEAD) == 1
            provider.generate.assert_not_awaited()
            async with database.sessions() as session:
                assert await ReviewRepository(session).get_review(multi_job.review_id) is None

    asyncio.run(run())


def test_advisory_lock_blocks_replacement_owner_and_releases_on_cancel(database, multi_job):
    if database.backend != "postgresql":
        pytest.skip("Ownership uses PostgreSQL advisory locks")

    async def run():
        started = asyncio.Event()

        async def hold():
            async with review_lock(database.engine, multi_job.review_id) as owned:
                assert owned
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await asyncio.wait_for(started.wait(), 2)
        async with review_lock(database.engine, multi_job.review_id) as owned:
            assert not owned
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with review_lock(database.engine, multi_job.review_id) as owned:
            assert owned

    asyncio.run(run())


def test_worker_crash_recovery_reopens_durable_checkpoint(
    database, settings, multi_job, multi_context
):
    if database.backend != "postgresql":
        pytest.skip("Worker crash recovery requires PostgreSQL checkpoints")
    configured = settings.model_copy(
        update={"database_url": PostgresDsn(os.environ["TEST_DATABASE_URL"])}
    )
    schema = database.schema + "_worker"
    calls = []

    async def run():
        try:
            async with FakeRedis(decode_responses=True) as redis:
                async with build_github_client(
                    settings, github_transport(multi_context, calls)
                ) as client:
                    provider = MockLLMProvider()
                    provider.generate = AsyncMock(wraps=provider.generate)
                    github = CachedGitHubService(GitHubService(client, settings), redis, settings)

                    def factory():
                        return open_postgres_checkpointer(configured, schema=schema)

                    queue = WorkerQueue(redis, settings)
                    await ReviewJobQueue(redis, 60).enqueue(multi_job)
                    receipt = await queue.claim()
                    await queue.begin(receipt)
                    async with factory() as saver:
                        paused = ReviewWorkflow(
                            reviewers=create_reviewers(provider, settings),
                            mock_mode=True,
                            sessions=database.sessions,
                            github=github,
                            checkpointer=saver,
                            stop_before_persist=True,
                        )
                        await paused.start(multi_job)
                        assert (await paused.report(multi_job.review_id)).pending_nodes == [
                            "persist"
                        ]
                    await redis.delete(f"review:{multi_job.review_id}:lease")
                    await redis.zadd(LEASES, {receipt.token: 0})
                    second = WorkflowProcessor(
                        database.engine,
                        database.sessions,
                        settings,
                        provider,
                        github,
                        checkpointers=factory,
                    )
                    await ReviewWorker(queue, second, settings).run_once()
                    assert (await queue.status(multi_job.review_id)).status == "COMPLETED"
                    assert provider.generate.await_count == 4
        finally:
            async with database.engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    with asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None
    ) as runner:
        runner.run(run())


def test_committed_worker_failure_prevents_pending_model_reexecution(
    database, settings, multi_job, multi_context
):
    async def run():
        provider = MockLLMProvider()
        provider.generate = AsyncMock(wraps=provider.generate)
        saver = InMemorySaver()
        workflow = ReviewWorkflow(
            reviewers=create_reviewers(provider, settings),
            mock_mode=True,
            sessions=database.sessions,
            checkpointer=saver,
            stop_before_persist=True,
        )
        await workflow.start(multi_job, multi_context)
        # Model work is durable, but worker failure is committed without its graph update.
        async with database.sessions.begin() as session:
            await ReviewRepository(session).fail_review(
                multi_job.review_id, "Worker attempts exhausted"
            )
        rebuilt = ReviewWorkflow(
            reviewers=create_reviewers(provider, settings),
            mock_mode=True,
            sessions=database.sessions,
            checkpointer=saver,
        )
        await rebuilt.resume(multi_job.review_id)
        report = await rebuilt.report(multi_job.review_id)
        assert report.status == "FAILED" and report.persisted and not report.paused
        assert report.review is None
        assert provider.generate.await_count == 4

    asyncio.run(run())


@pytest.mark.parametrize(
    "all_failed,expected,findings", [(False, "PARTIAL", 3), (True, "FAILED", 0)]
)
def test_worker_preserves_graph_partial_and_failed_outcomes(
    database, settings, multi_job, multi_context, all_failed, expected, findings
):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            async with build_github_client(settings, github_transport(multi_context, [])) as client:
                provider = MockLLMProvider()
                generate = provider.generate

                async def review(request):
                    if all_failed or request.schema_name == "quality_review":
                        raise LLMProviderError("Controlled provider failure")
                    return await generate(request)

                provider.generate = review
                processor = make_processor(
                    database,
                    settings,
                    provider,
                    CachedGitHubService(GitHubService(client, settings), redis, settings),
                    InMemorySaver(),
                )
                queue = WorkerQueue(redis, settings)
                await ReviewJobQueue(redis, 60).enqueue(multi_job)
                await ReviewWorker(queue, processor, settings).run_once()
                status = await queue.status(multi_job.review_id)
                assert status.status == expected and status.attempts == 1
                assert await redis.zcard(DELAYED) == 0 and await redis.llen(DEAD) == 0
                async with database.sessions() as session:
                    stored = await ReviewRepository(session).get_review(multi_job.review_id)
                    assert stored.status == expected and len(stored.findings) == findings

    asyncio.run(run())


def test_worker_exhaustion_finalizes_database_and_checkpoint(
    database, settings, multi_job, multi_context, monkeypatch
):
    async def fail_persist(self, state):
        raise WorkflowPersistenceError("private-failure")

    monkeypatch.setattr(ReviewNodes, "persist", fail_persist)

    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            async with build_github_client(settings, github_transport(multi_context, [])) as client:
                provider = MockLLMProvider()
                provider.generate = AsyncMock(wraps=provider.generate)
                saver = InMemorySaver()
                processor = make_processor(
                    database,
                    settings,
                    provider,
                    CachedGitHubService(GitHubService(client, settings), redis, settings),
                    saver,
                )
                queue = WorkerQueue(redis, settings)
                await ReviewJobQueue(redis, 60).enqueue(multi_job)
                for _ in range(settings.worker_max_attempts):
                    await ReviewWorker(queue, processor, settings).run_once()
                    tokens = await redis.zrange(DELAYED, 0, -1)
                    if tokens:
                        await redis.zadd(DELAYED, {token: 0 for token in tokens})
                assert (await queue.status(multi_job.review_id)).status == "FAILED"
                async with database.sessions() as session:
                    assert (
                        await ReviewRepository(session).get_review(multi_job.review_id)
                    ).status == "FAILED"
                report = await processor.workflow(saver).report(multi_job.review_id)
                assert report.status == "FAILED" and not report.paused
                assert provider.generate.await_count == 4 and await redis.llen(DEAD) == 1

    asyncio.run(run())
