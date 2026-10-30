import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from app.core.exceptions import GitHubRateLimitError, LeaseLostError, PullRequestChangedError
from app.schemas.review import ReviewStatus
from app.services.job_queue import ReviewJobQueue
from app.services.worker_queue import DEAD, DELAYED, LEASES, PROCESSING, WorkerQueue
from app.worker.runner import ReviewWorker, failure_reason


def processor(*, locked=True):
    @asynccontextmanager
    async def lock(job):
        yield locked

    return SimpleNamespace(
        lock=lock,
        terminal_status=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=ReviewStatus.COMPLETED),
        fail=AsyncMock(return_value=ReviewStatus.FAILED),
    )


def test_worker_completes_and_reconciles_already_committed_result(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            handler = processor()
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            worker = ReviewWorker(queue, handler, settings)
            assert await worker.run_once()
            assert (await queue.status(sample_job.review_id)).status == "COMPLETED"
            assert handler.execute.await_count == 1
            handler.terminal_status.return_value = ReviewStatus.COMPLETED
            await redis.rpush("reviews:pending", sample_job.model_dump_json())
            assert await worker.run_once()
            assert handler.execute.await_count == 1
            assert (await queue.status(sample_job.review_id)).attempts == 1
            assert not await worker.run_once()

    asyncio.run(run())


def test_transient_failures_retry_with_bound_and_safe_dead_letter(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            handler = processor()
            handler.execute.side_effect = OSError("private-network-details")
            queue = WorkerQueue(redis, settings)
            worker = ReviewWorker(queue, handler, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            for attempt in range(1, settings.worker_max_attempts + 1):
                assert await worker.run_once()
                status = await queue.status(sample_job.review_id)
                assert status.attempts == attempt
                assert "private-network-details" not in status.model_dump_json()
                if attempt < settings.worker_max_attempts:
                    assert status.status == "QUEUED" and status.next_retry_at
                    due = await redis.zrange(DELAYED, 0, -1)
                    await redis.zadd(DELAYED, {token: 0 for token in due})
            assert status.status == "FAILED"
            assert handler.fail.await_count == 1 and handler.execute.await_count == 3
            assert await redis.llen(DEAD) == 1
            assert "private-network-details" not in await redis.lindex(DEAD, 0)
            assert await redis.hlen(PROCESSING) == 0

    asyncio.run(run())


def test_stale_pr_fails_without_retry_or_provider_calls(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            handler = processor()
            handler.execute.side_effect = PullRequestChangedError("private-details")
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            await ReviewWorker(queue, handler, settings).run_once()
            assert (await queue.status(sample_job.review_id)).status == "FAILED"
            assert handler.execute.await_count == 1
            assert await redis.zcard(DELAYED) == 0

    asyncio.run(run())


def test_busy_database_owner_defers_without_consuming_attempt(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            handler = processor(locked=False)
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            await ReviewWorker(queue, handler, settings).run_once()
            assert (await queue.status(sample_job.review_id)).attempts == 0
            assert await redis.zcard(DELAYED) == 1
            handler.execute.assert_not_awaited()

    asyncio.run(run())


def test_lost_heartbeat_cancels_model_work_without_ack(settings, sample_job):
    async def run():
        configured = settings.model_copy(update={"worker_lease_seconds": 0.06})
        async with FakeRedis(decode_responses=True) as redis:
            started, cancelled = asyncio.Event(), asyncio.Event()
            handler = processor()

            async def blocked(job):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            handler.execute.side_effect = blocked
            queue = WorkerQueue(redis, configured)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            task = asyncio.create_task(ReviewWorker(queue, handler, configured).run_once())
            await asyncio.wait_for(started.wait(), 1)
            await redis.set(f"review:{sample_job.review_id}:lease", "replacement-owner")
            with pytest.raises(LeaseLostError):
                await asyncio.wait_for(task, 1)
            assert cancelled.is_set()
            assert await redis.hlen(PROCESSING) == 1
            assert (await queue.status(sample_job.review_id)).status == "PROCESSING"

    asyncio.run(run())


def test_shutdown_cancels_after_grace_and_receipt_is_recoverable(settings, sample_job):
    async def run():
        configured = settings.model_copy(update={"worker_shutdown_grace_seconds": 0})
        async with FakeRedis(decode_responses=True) as redis:
            started, cancelled, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
            handler = processor()

            async def blocked(job):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            handler.execute.side_effect = blocked
            queue = WorkerQueue(redis, configured)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            task = asyncio.create_task(ReviewWorker(queue, handler, configured).run_forever(stop))
            await asyncio.wait_for(started.wait(), 1)
            stop.set()
            await asyncio.wait_for(task, 1)
            assert cancelled.is_set() and await redis.hlen(PROCESSING) == 1
            token = (await redis.hkeys(PROCESSING))[0]
            await redis.delete(f"review:{sample_job.review_id}:lease")
            await redis.zadd(LEASES, {token: 0})
            handler.execute.side_effect = None
            assert await ReviewWorker(queue, handler, configured).run_once()
            assert (await queue.status(sample_job.review_id)).status == "COMPLETED"

    asyncio.run(run())


def test_job_timeout_is_retried_instead_of_returning_empty_success(settings, sample_job):
    async def run():
        configured = settings.model_copy(update={"worker_job_timeout_seconds": 0.02})
        async with FakeRedis(decode_responses=True) as redis:
            handler = processor()

            async def blocked(job):
                await asyncio.Event().wait()

            handler.execute.side_effect = blocked
            queue = WorkerQueue(redis, configured)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            await ReviewWorker(queue, handler, configured).run_once()
            status = await queue.status(sample_job.review_id)
            assert status.status == "QUEUED" and "deadline" in status.error

    asyncio.run(run())


def test_rate_limit_window_schedules_without_sleeping_worker(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            handler = processor()
            handler.execute.side_effect = GitHubRateLimitError("safe", 429, retry_after_seconds=900)
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            before = (await redis.time())[0]
            await ReviewWorker(queue, handler, settings).run_once()
            status = await queue.status(sample_job.review_id)
            assert status.status == "QUEUED" and status.next_retry_at.timestamp() >= before + 900
            assert await redis.hlen(PROCESSING) == 0

    asyncio.run(run())


def test_extreme_provider_delay_requires_new_delivery_instead_of_early_retry():
    error = GitHubRateLimitError("safe", 429, retry_after_seconds=604801)
    assert failure_reason(error)[1] is False
