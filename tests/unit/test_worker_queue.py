import asyncio
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis

from app.core.exceptions import LeaseLostError
from app.schemas.review import ReviewStatus
from app.services.job_queue import REVIEW_QUEUE_KEY, ReviewJobQueue
from app.services.worker_queue import DEAD, DELAYED, LEASES, PROCESSING, RETRY_JOBS, WorkerQueue


def test_claim_begin_heartbeat_and_ack_are_token_owned(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            queue = WorkerQueue(redis, settings)
            receipt = await queue.claim()
            assert receipt.job == sample_job
            assert await redis.llen(REVIEW_QUEUE_KEY) == 0
            assert await redis.hget(PROCESSING, receipt.token) == receipt.raw
            assert await queue.begin(receipt) == 1
            assert (await queue.status(sample_job.review_id)).status == "PROCESSING"
            await queue.renew(receipt)
            await queue.finish(receipt, ReviewStatus.COMPLETED)
            status = await queue.status(sample_job.review_id)
            assert status.status == "COMPLETED" and status.attempts == 1
            assert await redis.hlen(PROCESSING) == await redis.zcard(LEASES) == 0
            assert await redis.ttl(f"review:{sample_job.review_id}:status") > 0
            with pytest.raises(LeaseLostError):
                await queue.finish(receipt, ReviewStatus.FAILED)
            with pytest.raises(LeaseLostError):
                await queue.renew(receipt)

    asyncio.run(run())


def test_expired_claim_recovery_does_not_let_old_owner_ack(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            first = await queue.claim()
            await queue.begin(first)
            await redis.delete(f"review:{sample_job.review_id}:lease")
            await redis.zadd(LEASES, {first.token: 0})
            assert await queue.maintain() == 1
            second = await queue.claim()
            assert second.token != first.token
            assert await queue.begin(second) == 2
            with pytest.raises(LeaseLostError):
                await queue.defer(first, 0)
            await queue.finish(second, ReviewStatus.PARTIAL)
            assert (await queue.status(sample_job.review_id)).status == "PARTIAL"

    asyncio.run(run())


def test_live_heartbeat_prevents_reclaim_even_with_old_sorted_score(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            receipt = await queue.claim()
            await redis.zadd(LEASES, {receipt.token: 0})
            assert await queue.maintain() == 0
            assert await redis.hlen(PROCESSING) == 1
            assert await redis.llen(REVIEW_QUEUE_KEY) == 0

    asyncio.run(run())


def test_retry_jobs_survive_metadata_expiry_and_are_promoted_once(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            receipt = await queue.claim()
            await queue.begin(receipt)
            await queue.defer(receipt, 60, "safe transient error")
            status = await queue.status(sample_job.review_id)
            assert status.status == "QUEUED" and status.next_retry_at is not None
            await redis.delete(
                f"review:{sample_job.review_id}:job",
                f"review:{sample_job.review_id}:status",
                f"review:{sample_job.review_id}:metadata",
            )
            await redis.zadd(DELAYED, {receipt.token: 0})
            await asyncio.gather(queue.maintain(), queue.maintain())
            assert await redis.llen(REVIEW_QUEUE_KEY) == 1
            assert await redis.hlen(RETRY_JOBS) == 0
            second = await queue.claim()
            assert await queue.begin(second) == 2
            await queue.finish(second, ReviewStatus.COMPLETED)

    asyncio.run(run())


def test_duplicate_active_job_rotates_without_blocking_other_jobs(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            first = await queue.claim()
            other = sample_job.model_copy(update={"review_id": uuid4(), "delivery_id": uuid4()})
            await redis.rpush(
                REVIEW_QUEUE_KEY, sample_job.model_dump_json(), other.model_dump_json()
            )
            assert await queue.claim() is None
            second = await queue.claim()
            assert second.job == other
            assert first.token != second.token

    asyncio.run(run())


def test_concurrent_claimers_cannot_lose_or_share_a_receipt(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            queue = WorkerQueue(redis, settings)
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            results = await asyncio.gather(*(queue.claim() for _ in range(10)))
            assert sum(result is not None for result in results) == 1
            assert await redis.hlen(PROCESSING) == 1

    asyncio.run(run())


@pytest.mark.parametrize("raw", ["not json", "{}", "x" * 32769, '{"review_id":"../../escape"}'])
def test_bad_envelope_is_quarantined_without_source_leak(settings, raw):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            await redis.rpush(REVIEW_QUEUE_KEY, raw)
            assert await WorkerQueue(redis, settings).claim() is None
            assert await redis.llen(REVIEW_QUEUE_KEY) == 0
            stored = await redis.lindex(DEAD, 0)
            assert "sha256" in stored
            assert raw not in stored

    asyncio.run(run())


def test_invalid_binary_queue_entry_is_quarantined(settings):
    async def run():
        async with FakeRedis(decode_responses=False) as redis:
            await redis.rpush(REVIEW_QUEUE_KEY, b"\xff\x00invalid")
            assert await WorkerQueue(redis, settings).claim() is None
            assert await redis.llen(DEAD) == 1

    asyncio.run(run())


def test_wrong_key_type_does_not_pop_pending_job(settings, sample_job):
    from redis.exceptions import ResponseError

    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            await ReviewJobQueue(redis, 60).enqueue(sample_job)
            await redis.set(PROCESSING, "incorrect type")
            with pytest.raises(ResponseError):
                await WorkerQueue(redis, settings).claim()
            assert await redis.llen(REVIEW_QUEUE_KEY) == 1

    asyncio.run(run())


@pytest.mark.parametrize("payload_key,sorted_key", [(PROCESSING, LEASES), (RETRY_JOBS, DELAYED)])
def test_corrupt_durable_envelope_does_not_block_maintenance(settings, payload_key, sorted_key):
    async def run():
        async with FakeRedis(decode_responses=False) as redis:
            await redis.hset(payload_key, "receipt", b"\xffinvalid")
            await redis.zadd(sorted_key, {"receipt": 0})
            assert await WorkerQueue(redis, settings).maintain() == 1
            assert await redis.hlen(payload_key) == 0
            assert await redis.llen(DEAD) == 1

    asyncio.run(run())
