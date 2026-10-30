"""Exercise the entire delivery path; only remote HTTP and checkpoints are fixtures."""

import asyncio
import json
import os
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PostgresDsn, SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text

from app.db.models import Finding, Review, ReviewPublication
from app.graph.checkpoints import open_postgres_checkpointer
from app.services.github_service import GitHubService, build_github_client
from app.services.llm_service import LLMRequest, MockLLMProvider, build_llm_provider
from app.services.publication_service import ReviewPublisher
from app.services.worker_queue import DELAYED, LEASES, PROCESSING, WorkerQueue
from app.worker.processor import WorkflowProcessor
from app.worker.runner import ReviewWorker
from tests.support.github import pr_payload, webhook_headers


class RemoteFixtures:
    def __init__(self, context, scenario):
        self.context, self.scenario = context, scenario
        self.models = Counter()
        self.comments = []
        self.posts = 0

    async def model(self, request):
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        schema = payload["response_format"]["json_schema"]
        role = schema["name"].removesuffix("_review")
        self.models[role] += 1
        messages = payload["messages"]
        original = LLMRequest(
            messages[0]["content"], messages[1]["content"], schema["name"], schema["schema"]
        )
        if self.scenario == "clean":
            content = json.dumps(
                {"findings": [], "summary": "No supported findings.", "risk_score": 0}
            )
        elif (
            self.scenario == "all_failed"
            or self.scenario == "partial"
            and role == "security"
            or self.scenario == "repair"
            and role == "security"
            and self.models[role] == 1
        ):
            content = "invalid-json-private-model-content"
        else:
            content = await MockLLMProvider().generate(original)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    def github(self, request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17})
        if request.url.path.endswith("/files"):
            files = [file.model_dump() for file in self.context.changed_files]
            if self.scenario == "missing_patch":
                files[0]["patch"] = None
            return httpx.Response(200, json=files)
        if "/pulls/" in request.url.path:
            payload = pr_payload(self.context)
            if self.scenario == "stale":
                payload["head"]["sha"] = "d" * 40
            return httpx.Response(200, json=payload)
        assert request.url.path.endswith("/comments") and "/issues/" in request.url.path
        if request.method == "GET":
            return httpx.Response(200, json=self.comments)
        assert request.method == "POST"
        self.posts += 1
        if self.scenario == "rate_limit" and self.posts == 1:
            return httpx.Response(429, headers={"Retry-After": "60"})
        comment = {"id": 101, "body": json.loads(request.content)["body"], "user": {"id": 17}}
        self.comments.append(comment)
        if self.scenario == "lost_response":
            raise httpx.ReadTimeout("private-response-lost")
        return httpx.Response(201, json=comment)


class SQLiteProcessor(WorkflowProcessor):
    @asynccontextmanager
    async def lock(self, job):
        # SQLite verifies business I/O only. PostgreSQL uses the real advisory lock.
        yield True


@asynccontextmanager
async def delivery_worker(database, settings, backend, fixtures):
    configured = settings.model_copy(
        update={
            "llm_mock_mode": False,
            "llm_base_url": "https://model.invalid/v1/",
            "llm_model": "fixture-model",
            "llm_max_attempts": 1,
            "github_token": SecretStr("fixture-token"),
            "github_publish_enabled": True,
            "github_max_attempts": 1,
        }
    )
    saver = InMemorySaver()
    checkpoint_schema = database.schema + "_workflow"

    @asynccontextmanager
    async def checkpoints():
        if database.backend == "postgresql":
            durable = configured.model_copy(
                update={"database_url": PostgresDsn(os.environ["TEST_DATABASE_URL"])}
            )
            async with open_postgres_checkpointer(durable, schema=checkpoint_schema) as postgres:
                yield postgres
        else:
            yield saver

    async with backend.connect() as redis:
        async with build_github_client(configured, httpx.MockTransport(fixtures.github)) as client:
            github = GitHubService(client, configured)
            async with build_llm_provider(
                configured, httpx.MockTransport(fixtures.model)
            ) as provider:
                processor_type = (
                    WorkflowProcessor if database.backend == "postgresql" else SQLiteProcessor
                )
                processor = processor_type(
                    database.engine,
                    database.sessions,
                    configured,
                    provider,
                    github,
                    checkpointers=checkpoints,
                    publisher=ReviewPublisher(database.sessions, github, configured),
                )
                queue = WorkerQueue(redis, configured, namespace=backend.namespace)
                try:
                    yield ReviewWorker(queue, processor, configured), queue, redis, processor
                finally:
                    if database.backend == "postgresql":
                        async with database.engine.begin() as connection:
                            await connection.execute(
                                text(f'DROP SCHEMA IF EXISTS "{checkpoint_schema}" CASCADE')
                            )


def run_async(coroutine):
    with asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None
    ) as runner:
        return runner.run(coroutine)


def accept(client, context):
    body = json.dumps(
        {
            "action": "opened",
            "number": context.pr_number,
            "repository": {"full_name": context.repository},
            "pull_request": pr_payload(context),
        }
    ).encode()
    signed = webhook_headers(body)
    rejected = client.post(
        "/webhooks/github", content=body, headers=webhook_headers(body, secret="wrong")
    )
    assert rejected.status_code == 403
    response = client.post("/webhooks/github", content=body, headers=signed)
    assert response.status_code == 202 and not response.json()["duplicate"]
    review_id = response.json()["review_id"]
    assert client.get(f"/reviews/{review_id}/status").json()["status"] == "QUEUED"
    return review_id, body, signed


@pytest.mark.parametrize(
    "scenario,status,count,recommendation,calls",
    [
        ("findings", "COMPLETED", 4, "REQUEST_CHANGES", 4),
        ("clean", "COMPLETED", 0, "APPROVE", 4),
        ("partial", "PARTIAL", 3, "COMMENT", 5),
        ("repair", "COMPLETED", 4, "REQUEST_CHANGES", 5),
        ("missing_patch", "PARTIAL", 3, "COMMENT", 4),
        ("all_failed", "FAILED", 0, None, 8),
        ("stale", "FAILED", 0, None, 0),
    ],
)
def test_signed_delivery_to_final_comment(
    workflow_client,
    database,
    settings,
    queue_backend,
    multi_context,
    scenario,
    status,
    count,
    recommendation,
    calls,
):
    client = workflow_client
    review_id, body, signed = accept(client, multi_context)
    fixtures = RemoteFixtures(multi_context, scenario)

    async def run():
        async with delivery_worker(database, settings, queue_backend, fixtures) as (
            worker,
            queue,
            redis,
            _,
        ):
            assert await worker.run_once()
            assert not await worker.run_once()
            assert (await queue.status(UUID(review_id))).status == status
            assert await redis.hlen(queue_backend.namespace + PROCESSING) == 0
            async with database.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Finding)) == count
                assert await session.scalar(select(func.count()).select_from(Review)) == (
                    scenario != "stale"
                )

    run_async(run())
    assert sum(fixtures.models.values()) == calls
    stored = client.get(f"/reviews/{review_id}")
    if scenario == "stale":
        assert stored.status_code == 404
    else:
        assert stored.status_code == 200
        result = stored.json()
        assert result["status"] == status and result["recommendation"] == recommendation
        assert len(result["findings"]) == count
        if scenario in {"findings", "repair"}:
            assert result["risk_score"] == pytest.approx(7.7)
            assert {finding["agent_type"] for finding in result["findings"]} == {
                "security",
                "quality",
                "testing",
                "documentation",
            }
    if status == "FAILED":
        assert fixtures.posts == 0 and not fixtures.comments
    else:
        assert fixtures.posts == 1 and len(fixtures.comments) == 1
        preview = client.get(f"/reviews/{review_id}/comment")
        assert preview.status_code == 200 and preview.text == fixtures.comments[0]["body"]
        assert client.get(f"/reviews/{review_id}/publication").json()["status"] == "PUBLISHED"
        assert recommendation.replace("_", " ").title() in preview.text
    repeat = client.post("/webhooks/github", content=body, headers=signed)
    assert repeat.status_code == 202 and repeat.json()["duplicate"]
    assert repeat.json()["review_id"] == review_id and repeat.json()["status"] == status
    assert "private-" not in stored.text


@pytest.mark.parametrize("scenario", ["rate_limit", "lost_response"])
def test_publication_recovery_reuses_persisted_analysis(
    workflow_client,
    database,
    settings,
    queue_backend,
    multi_context,
    scenario,
):
    client = workflow_client
    review_id, _, _ = accept(client, multi_context)
    fixtures = RemoteFixtures(multi_context, scenario)

    async def run():
        async with delivery_worker(database, settings, queue_backend, fixtures) as (
            worker,
            queue,
            redis,
            processor,
        ):
            assert await worker.run_once()
            assert client.get(f"/reviews/{review_id}").json()["status"] == "COMPLETED"
            publication = client.get(f"/reviews/{review_id}/publication").json()
            if scenario == "rate_limit":
                assert publication["status"] == "PENDING"
                assert (await queue.status(UUID(review_id))).status == "QUEUED"
                tokens = await redis.zrange(queue_backend.namespace + DELAYED, 0, -1)
                assert len(tokens) == 1
                # Advance the two durable retry clocks, without wall-clock sleeps.
                async with database.sessions.begin() as session:
                    row = await session.get(ReviewPublication, UUID(review_id))
                    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
                await redis.zadd(queue_backend.namespace + DELAYED, {tokens[0]: 0})
                assert await worker.run_once()
                assert fixtures.posts == 2
            else:
                assert publication["status"] == "UNCERTAIN"
                # A fresh publisher reconciles the frozen body and authenticated author.
                async with processor.lock(SimpleNamespace(review_id=UUID(review_id))) as owned:
                    assert owned
                    publisher = ReviewPublisher(
                        database.sessions, processor.publisher.github, processor.settings
                    )
                    reconciled = await publisher.publish_owned(UUID(review_id))
                assert reconciled.status == "PUBLISHED" and fixtures.posts == 1
            assert (await queue.status(UUID(review_id))).status == "COMPLETED"
            assert sum(fixtures.models.values()) == 4
            assert len(fixtures.comments) == 1
            assert await redis.hlen(queue_backend.namespace + PROCESSING) == 0
            assert not await worker.run_once()

    run_async(run())
    assert client.get(f"/reviews/{review_id}/publication").json()["status"] == "PUBLISHED"
    assert client.get(f"/reviews/{review_id}/comment").text == fixtures.comments[0]["body"]


def test_crash_after_publishing_before_ack_recovers_without_duplicate_work(
    workflow_client,
    database,
    settings,
    queue_backend,
    multi_context,
    monkeypatch,
):
    client = workflow_client
    review_id, _, _ = accept(client, multi_context)
    fixtures = RemoteFixtures(multi_context, "findings")

    async def run():
        async with delivery_worker(database, settings, queue_backend, fixtures) as (
            worker,
            queue,
            redis,
            _,
        ):

            async def disconnected(*args, **kwargs):
                raise RedisConnectionError("simulated-ack-outage")

            monkeypatch.setattr(queue, "finish", disconnected)
            with pytest.raises(RedisConnectionError, match="simulated-ack-outage"):
                await worker.run_once()
            assert client.get(f"/reviews/{review_id}/publication").json()["status"] == "PUBLISHED"
            assert client.get(f"/reviews/{review_id}").json()["status"] == "COMPLETED"
            assert await redis.hlen(queue_backend.namespace + PROCESSING) == 1
            tokens = await redis.zrange(queue_backend.namespace + LEASES, 0, -1)
            assert len(tokens) == 1
            await redis.delete(queue_backend.namespace + f"review:{review_id}:lease")
            await redis.zadd(queue_backend.namespace + LEASES, {tokens[0]: 0})
        # Rebuild clients, graph/checkpointer factory, publisher, queue, and worker.
        # The terminal DB outcome and publication record alone must prevent repeats.
        async with delivery_worker(database, settings, queue_backend, fixtures) as (
            worker,
            queue,
            redis,
            _,
        ):
            assert await worker.run_once()
            assert not await worker.run_once()
            outcome = await queue.status(UUID(review_id))
            assert outcome.status == "COMPLETED" and outcome.attempts == 1
            assert await redis.hlen(queue_backend.namespace + PROCESSING) == 0
            assert fixtures.posts == 1 and len(fixtures.comments) == 1
            assert sum(fixtures.models.values()) == 4
            async with database.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Review)) == 1
                assert await session.scalar(select(func.count()).select_from(Finding)) == 4

    run_async(run())
