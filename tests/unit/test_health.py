import asyncio

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.main import create_app


def test_healthy_dependencies(client, resources):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "dependencies": {"postgres": "up", "pgvector": "up", "redis": "up"},
    }
    _, connection, redis = resources
    connection.scalar.assert_awaited_once()
    redis.ping.assert_awaited_once()


def test_database_failure_is_reported_without_exposing_details(client, resources):
    engine, _, redis = resources
    engine.connect.side_effect = OperationalError("SELECT", {}, Exception("private-password"))
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["dependencies"] == {
        "postgres": "down",
        "pgvector": "down",
        "redis": "up",
    }
    assert "private-password" not in response.text
    redis.ping.assert_awaited_once()


def test_missing_vector_extension_is_unhealthy(client, resources):
    _, connection, _ = resources
    connection.scalar.return_value = False
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["dependencies"] == {"postgres": "up", "pgvector": "down", "redis": "up"}


def test_redis_failure_and_recovery(client, resources):
    _, _, redis = resources
    redis.ping.side_effect = RedisConnectionError("private-host")
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["dependencies"]["redis"] == "down"
    assert "private-host" not in response.text
    redis.ping.side_effect = None
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("dependency", ["postgres", "redis"])
def test_slow_dependencies_are_bounded_by_timeout(client, resources, dependency):
    async def slow_operation(*args, **kwargs):
        await asyncio.sleep(10)

    _, connection, redis = resources
    operation = connection.scalar if dependency == "postgres" else redis.ping
    operation.side_effect = slow_operation
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["dependencies"][dependency] == "down"


def test_checks_run_concurrently(client, resources):
    postgres_started = asyncio.Event()
    redis_started = asyncio.Event()

    async def database_query(*args, **kwargs):
        postgres_started.set()
        await redis_started.wait()
        return True

    async def redis_ping():
        redis_started.set()
        await postgres_started.wait()
        return True

    _, connection, redis = resources
    connection.scalar.side_effect = database_query
    redis.ping.side_effect = redis_ping
    assert client.get("/health").status_code == 200


def test_lifespan_closes_resources(settings, resources):
    engine, _, redis = resources
    with TestClient(create_app(settings)):
        engine.dispose.assert_not_awaited()
        redis.aclose.assert_not_awaited()
    engine.dispose.assert_awaited_once()
    redis.aclose.assert_awaited_once()


def test_swagger_and_openapi_are_available(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/health" in schema["paths"]
    assert "503" in schema["paths"]["/health"]["get"]["responses"]
