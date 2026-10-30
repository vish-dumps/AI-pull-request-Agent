import asyncio
from uuid import uuid4

import fakeredis
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.main import create_app
from app.services.job_queue import ReviewJobQueue


def test_status_api_reads_queue_without_a_database(settings, resources, monkeypatch, sample_job):
    server = fakeredis.FakeServer()
    redis = FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr("app.main.Redis.from_url", lambda *args, **kwargs: redis)
    with TestClient(create_app(settings)) as client:
        with fakeredis.FakeRedis(server=server, decode_responses=True) as sync:
            sync.set(f"review:{sample_job.review_id}:status", "PROCESSING", ex=60)
            sync.hset(
                f"review:{sample_job.review_id}:metadata",
                mapping={"attempts": "2", "error": "", "retry_at": ""},
            )
            response = client.get(f"/reviews/{sample_job.review_id}/status")
            assert response.status_code == 200
            assert response.json()["status"] == "PROCESSING" and response.json()["attempts"] == 2
            assert client.get(f"/reviews/{uuid4()}/status").status_code == 404
            assert client.get("/reviews/bad-uuid/status").status_code == 422
            sync.set(f"review:{sample_job.review_id}:status", "private-bad-status")
            response = client.get(f"/reviews/{sample_job.review_id}/status")
            assert response.status_code == 503 and "private-bad-status" not in response.text


def test_status_api_reports_redis_outage(client, resources):
    from unittest.mock import AsyncMock

    resources[2].eval = AsyncMock(side_effect=RedisConnectionError("private-host"))
    response = client.get(f"/reviews/{uuid4()}/status")
    assert response.status_code == 503 and "private-host" not in response.text


def test_redelivery_does_not_regress_terminal_job_status(settings, sample_job):
    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            queue = ReviewJobQueue(redis, 60)
            await queue.enqueue(sample_job)
            await redis.set(f"review:{sample_job.review_id}:status", "PARTIAL", ex=60)
            response = await queue.enqueue(sample_job.model_copy(update={"review_id": uuid4()}))
            assert response.duplicate and response.status == "PARTIAL"
            assert await redis.llen("reviews:pending") == 1

    asyncio.run(run())
