import asyncio
from uuid import uuid4

from fakeredis.aioredis import FakeRedis

from app.schemas.jobs import ReviewJob
from app.services.job_queue import REVIEW_QUEUE_KEY, ReviewJobQueue


def test_concurrent_redeliveries_are_deduplicated_atomically(sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            queue = ReviewJobQueue(redis, ttl_seconds=60)
            jobs = [sample_job.model_copy(update={"review_id": uuid4()}) for _ in range(10)]
            results = await asyncio.gather(*(queue.enqueue(job) for job in jobs))
            assert len({result.review_id for result in results}) == 1
            assert sum(not result.duplicate for result in results) == 1
            assert await redis.llen(REVIEW_QUEUE_KEY) == 1

    asyncio.run(run())


def test_expired_metadata_does_not_remove_pending_job(sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            await ReviewJobQueue(redis, ttl_seconds=60).enqueue(sample_job)
            await redis.delete(f"review:{sample_job.review_id}:job")
            await redis.delete(f"review:{sample_job.review_id}:status")
            queued = ReviewJob.model_validate_json(await redis.lpop(REVIEW_QUEUE_KEY))
            assert queued == sample_job

    asyncio.run(run())
