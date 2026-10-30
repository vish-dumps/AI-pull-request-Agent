import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core.exceptions import LeaseLostError
from app.schemas.review import ReviewStatus
from app.services.job_queue import ReviewJobQueue
from app.services.worker_queue import DELAYED, LEASES, PROCESSING, WorkerQueue


def test_real_redis_receipts_retries_recovery_and_token_fencing(settings, sample_job):
    if not os.getenv("TEST_REDIS_URL"):
        pytest.skip("Real Redis queue scripts require TEST_REDIS_URL")
    namespace = "test:" + uuid4().hex + ":"

    async def run():
        async with Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True) as redis:
            try:
                queue = WorkerQueue(redis, settings, namespace=namespace)
                producer = ReviewJobQueue(redis, 60, namespace=namespace)
                await producer.enqueue(sample_job)
                first = await queue.claim()
                assert await queue.begin(first) == 1
                await queue.renew(first)
                await queue.defer(first, 60, "safe retry")
                await redis.zadd(namespace + DELAYED, {first.token: 0})
                await asyncio.gather(queue.maintain(), queue.maintain())
                second = await queue.claim()
                assert await queue.begin(second) == 2
                await redis.delete(namespace + f"review:{sample_job.review_id}:lease")
                await redis.zadd(namespace + LEASES, {second.token: 0})
                await queue.maintain()
                third = await queue.claim()
                with pytest.raises(LeaseLostError):
                    await queue.finish(second, ReviewStatus.COMPLETED)
                assert await queue.begin(third) == 3
                await queue.finish(third, ReviewStatus.COMPLETED)
                assert (await queue.status(sample_job.review_id)).status == "COMPLETED"
                assert await redis.hlen(namespace + PROCESSING) == 0
            finally:
                keys = [key async for key in redis.scan_iter(match=namespace + "*")]
                if keys:
                    await redis.delete(*keys)

    asyncio.run(run())
