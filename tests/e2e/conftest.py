import asyncio
import os
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import fakeredis
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from redis.asyncio import ConnectionPool, Redis

from app.main import create_app
from app.services.job_queue import ReviewJobQueue
from app.services.worker_queue import WorkerQueue


@pytest.fixture(params=["fake"] + (["redis"] if os.getenv("TEST_REDIS_URL") else []))
def queue_backend(request, monkeypatch):
    """Use production Lua in either Redis backend, always under a unique prefix."""
    namespace = "test:" + uuid4().hex + ":"
    if request.param == "redis":

        def connect():
            return Redis.from_pool(
                ConnectionPool.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
            )
    else:
        server = fakeredis.FakeServer()

        def connect():
            return FakeRedis(server=server, decode_responses=True)

    # The application defaults to unprefixed queues. Only select a test namespace;
    # acceptance, Lua scripts, receipt handling, and status reads stay unchanged.
    monkeypatch.setattr("app.main.Redis.from_url", lambda *args, **kwargs: connect())
    monkeypatch.setattr(
        "app.api.routes.webhooks.ReviewJobQueue", partial(ReviewJobQueue, namespace=namespace)
    )
    monkeypatch.setattr(
        "app.api.routes.jobs.WorkerQueue", partial(WorkerQueue, namespace=namespace)
    )
    yield SimpleNamespace(connect=connect, namespace=namespace)

    async def cleanup():
        async with connect() as redis:
            keys = [key async for key in redis.scan_iter(match=namespace + "*")]
            if keys:
                await redis.delete(*keys)

    asyncio.run(cleanup())


@pytest.fixture
def workflow_client(settings, resources, database, queue_backend):
    with TestClient(create_app(settings)) as client:
        client.app.state.session_factory = database.sessions
        yield client
