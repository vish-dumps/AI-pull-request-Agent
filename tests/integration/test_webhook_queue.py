import asyncio
import hashlib
import hmac
import json
from uuid import UUID, uuid4

import fakeredis
import pytest
from fakeredis.aioredis import FakeRedis as FakeAsyncRedis
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError

from app.main import create_app
from app.schemas.jobs import ReviewJob
from app.services.job_queue import REVIEW_QUEUE_KEY


@pytest.fixture
def webhook_client(settings, resources, monkeypatch):
    server = fakeredis.FakeServer()
    redis = FakeAsyncRedis(server=server, decode_responses=True, version=(7, 4))
    monkeypatch.setattr("app.main.Redis.from_url", lambda *args, **kwargs: redis)
    with fakeredis.FakeRedis(server=server, decode_responses=True) as inspection:
        with TestClient(create_app(settings)) as client:
            yield client, inspection


def headers(body, secret="test-webhook-secret", event="pull_request", delivery=None):
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(delivery or uuid4()),
        "X-Hub-Signature-256": "sha256="
        + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
    }


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_supported_signed_delivery_enqueues_typed_job(webhook_client, sample_payload, action):
    client, redis = webhook_client
    sample_payload["action"] = action
    body = json.dumps(sample_payload).encode()
    response = client.post("/webhooks/github", content=body, headers=headers(body))
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "QUEUED"
    assert accepted["duplicate"] is False
    review_id = str(UUID(accepted["review_id"]))
    job = ReviewJob.model_validate_json(redis.lindex(REVIEW_QUEUE_KEY, 0))
    assert str(job.review_id) == review_id
    assert job.repository == "octocat/Hello-World"
    assert job.action == action
    assert job.base_sha == "a" * 40
    assert job.head_sha == "b" * 40
    assert redis.get(f"review:{review_id}:status") == "QUEUED"
    assert 0 < redis.ttl(f"review:{review_id}:job") <= 86400
    assert 0 < redis.ttl(f"github:delivery:{job.delivery_id}") <= 86400
    assert redis.ttl(REVIEW_QUEUE_KEY) == -1


def test_redelivery_returns_same_job_without_a_second_queue_entry(webhook_client, sample_payload):
    client, redis = webhook_client
    body = json.dumps(sample_payload).encode()
    signed = headers(body)
    first = client.post("/webhooks/github", content=body, headers=signed)
    second = client.post("/webhooks/github", content=body, headers=signed)
    assert first.status_code == second.status_code == 202
    assert first.json()["review_id"] == second.json()["review_id"]
    assert second.json()["duplicate"] is True
    assert redis.llen(REVIEW_QUEUE_KEY) == 1


def test_synchronize_with_new_delivery_is_a_new_job(webhook_client, sample_payload):
    client, redis = webhook_client
    body = json.dumps(sample_payload).encode()
    first = client.post("/webhooks/github", content=body, headers=headers(body))
    sample_payload["action"] = "synchronize"
    sample_payload["pull_request"]["head"]["sha"] = "c" * 40
    body = json.dumps(sample_payload).encode()
    second = client.post("/webhooks/github", content=body, headers=headers(body))
    assert first.json()["review_id"] != second.json()["review_id"]
    assert redis.llen(REVIEW_QUEUE_KEY) == 2


@pytest.mark.parametrize(
    "event,action", [("ping", None), ("push", None), ("pull_request", "closed")]
)
def test_unsupported_events_are_acknowledged_without_a_job(webhook_client, event, action):
    client, redis = webhook_client
    body = json.dumps({"action": action}).encode()
    response = client.post("/webhooks/github", content=body, headers=headers(body, event=event))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert redis.llen(REVIEW_QUEUE_KEY) == 0


@pytest.mark.parametrize("case", ["missing", "wrong", "modified_body", "non_ascii"])
def test_invalid_signatures_have_no_side_effects(webhook_client, sample_payload, case):
    client, redis = webhook_client
    body = json.dumps(sample_payload).encode()
    signed = headers(body)
    if case == "missing":
        signed.pop("X-Hub-Signature-256")
    elif case == "wrong":
        signed["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    elif case == "modified_body":
        body += b"\n"
    else:
        # Send raw header bytes so the HTTP client does not reject Latin-1 itself.
        signed = [(key.encode(), value.encode()) for key, value in signed.items()]
        signed = [
            (key, b"sha256=\xff" if key == b"X-Hub-Signature-256" else value)
            for key, value in signed
        ]
    response = client.post("/webhooks/github", content=body, headers=signed)
    assert response.status_code == 403
    assert redis.llen(REVIEW_QUEUE_KEY) == 0


@pytest.mark.parametrize(
    "body,status", [(b"not-json", 400), (b"[]", 422), (b'{"action":"opened"}', 422), (b"{}", 422)]
)
def test_invalid_signed_payload_is_rejected(webhook_client, body, status):
    client, redis = webhook_client
    response = client.post("/webhooks/github", content=body, headers=headers(body))
    assert response.status_code == status
    assert redis.llen(REVIEW_QUEUE_KEY) == 0


@pytest.mark.parametrize("delivery", [None, "not-a-uuid"])
def test_delivery_id_is_required_and_validated(webhook_client, sample_payload, delivery):
    client, redis = webhook_client
    body = json.dumps(sample_payload).encode()
    signed = headers(body)
    if delivery is None:
        signed.pop("X-GitHub-Delivery")
    else:
        signed["X-GitHub-Delivery"] = delivery
    response = client.post("/webhooks/github", content=body, headers=signed)
    assert response.status_code == 422
    assert redis.llen(REVIEW_QUEUE_KEY) == 0


def test_unconfigured_secret_fails_closed(webhook_client):
    client, redis = webhook_client
    client.app.state.settings.github_webhook_secret = SecretStr("")
    response = client.post("/webhooks/github", content=b"{}")
    assert response.status_code == 503
    assert redis.llen(REVIEW_QUEUE_KEY) == 0


def test_oversized_chunked_body_is_rejected(webhook_client):
    client, redis = webhook_client
    client.app.state.settings.max_webhook_body_bytes = 16
    response = client.post(
        "/webhooks/github",
        content=iter([b"a" * 10, b"b" * 10]),
        headers=headers(b"a" * 10 + b"b" * 10),
    )
    assert response.status_code == 413
    assert redis.llen(REVIEW_QUEUE_KEY) == 0


def test_wrong_content_type_is_rejected(webhook_client):
    client, _ = webhook_client
    body = b"{}"
    signed = headers(body)
    signed["Content-Type"] = "application/x-www-form-urlencoded"
    assert client.post("/webhooks/github", content=body, headers=signed).status_code == 415


def test_queue_unavailable_is_not_reported_as_accepted(webhook_client, sample_payload, monkeypatch):
    client, _ = webhook_client

    async def unavailable(*args, **kwargs):
        raise RedisConnectionError("private-password")

    monkeypatch.setattr(client.app.state.redis, "eval", unavailable)
    body = json.dumps(sample_payload).encode()
    response = client.post("/webhooks/github", content=body, headers=headers(body))
    assert response.status_code == 503
    assert "private-password" not in response.text


def test_enqueue_timeout_is_bounded(webhook_client, sample_payload, monkeypatch):
    client, _ = webhook_client
    client.app.state.settings.webhook_enqueue_timeout_seconds = 0.02

    async def stalled(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(client.app.state.redis, "eval", stalled)
    body = json.dumps(sample_payload).encode()
    assert client.post("/webhooks/github", content=body, headers=headers(body)).status_code == 503


def test_queue_type_error_does_not_leave_a_dedup_marker(webhook_client, sample_payload):
    client, redis = webhook_client
    redis.set(REVIEW_QUEUE_KEY, "invalid-queue-type")
    body = json.dumps(sample_payload).encode()
    signed = headers(body)
    response = client.post("/webhooks/github", content=body, headers=signed)
    assert response.status_code == 503
    assert redis.get(f"github:delivery:{signed['X-GitHub-Delivery']}") is None
