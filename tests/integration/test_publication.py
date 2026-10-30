import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.db.models import Review, ReviewPublication
from app.db.repositories import ReviewRepository
from app.main import create_app
from app.services.github_service import GitHubService, build_github_client
from app.services.publication_service import ReviewPublisher


def publishing_settings(settings, **updates):
    return settings.model_copy(
        update={
            "github_publish_enabled": True,
            "github_token": SecretStr("fixture-token"),
            "github_max_attempts": 1,
            **updates,
        }
    )


async def seed(database, job, context, result, *, mode=False):
    async with database.sessions.begin() as session:
        store = ReviewRepository(session)
        await store.create_review(job, context, mock_mode=mode)
        await store.mark_processing(job.review_id)
        await store.complete_review(job.review_id, result)


class GitHubStub:
    def __init__(self, context):
        self.context = context
        self.actor = 17
        self.comments = []
        self.post_calls = 0
        self.calls = []
        self.behavior = "normal"
        self.state = "open"
        self.head = context.head_sha
        self.base = context.base_sha
        self.full_history = True
        self.get_failure = False
        self.change_on_second_read = False
        self.pr_reads = 0

    def __call__(self, request):
        self.calls.append(request)
        if request.url.path == "/user":
            if self.get_failure:
                return httpx.Response(500, json={"error": "private-api-detail"})
            return httpx.Response(200, json={"id": self.actor, "login": "review-bot"})
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200, json=[file.model_dump() for file in self.context.changed_files]
            )
        if "/pulls/" in request.url.path:
            self.pr_reads += 1
            head = "c" * 40 if self.change_on_second_read and self.pr_reads > 1 else self.head
            return httpx.Response(
                200,
                json={
                    "number": self.context.pr_number,
                    "title": "PR",
                    "body": "",
                    "state": self.state,
                    "merged": False,
                    "user": {"login": "developer"},
                    "base": {
                        "ref": "main",
                        "sha": self.base,
                        "repo": {"full_name": self.context.repository},
                    },
                    "head": {
                        "ref": "branch",
                        "sha": head,
                        "repo": {"full_name": self.context.repository},
                    },
                },
            )
        assert "/issues/" in request.url.path and request.url.path.endswith("/comments")
        if request.method == "GET":
            headers = (
                {} if self.full_history else {"Link": '<https://attacker.invalid>; rel="next"'}
            )
            return httpx.Response(200, json=self.comments, headers=headers)
        self.post_calls += 1
        if self.behavior == "rate_once" and self.post_calls == 1:
            return httpx.Response(
                429, json={"error": "private-rate-body"}, headers={"Retry-After": "90"}
            )
        if self.behavior == "forbidden":
            return httpx.Response(403, json={"error": "private-denied-body"})
        if self.behavior == "timeout_before":
            raise httpx.ReadTimeout("private-transport-detail")
        body = json.loads(request.content)["body"]
        comment = {"id": 40 + self.post_calls, "body": body, "user": {"id": self.actor}}
        self.comments.append(comment)
        if self.behavior == "timeout_after":
            raise httpx.ReadTimeout("private-transport-detail")
        if self.behavior == "server_after":
            return httpx.Response(500, json={"error": "private-server-body"})
        if self.behavior == "malformed_success":
            return httpx.Response(201, json={"id": "invalid-id"})
        return httpx.Response(201, json=comment)


def test_publish_once_reuses_frozen_record_and_leaves_analysis_unchanged(
    database, settings, sample_job, sample_context, sample_result
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            publisher = ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            )
            first = await publisher.publish_owned(sample_job.review_id)
            second = await publisher.publish_owned(sample_job.review_id)
        assert first.status == second.status == "PUBLISHED" and first.comment_id == 41
        assert stub.post_calls == 1 and first.post_attempts == 1
        assert stub.pr_reads == 2
        async with database.sessions() as session:
            row = await session.get(ReviewPublication, sample_job.review_id)
            stored = await ReviewRepository(session).get_review(sample_job.review_id)
            assert row.body == stub.comments[0]["body"]
            assert stored.status == "COMPLETED" and len(stored.findings) == 1
            assert stored.mock_mode is False

    asyncio.run(run())


@pytest.mark.parametrize("behavior", ["timeout_after", "server_after", "malformed_success"])
def test_uncertain_post_reconciles_owned_comment_without_resending(
    database, settings, sample_job, sample_context, sample_result, behavior
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        stub.behavior = behavior
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            publisher = ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            )
            first = await publisher.publish_owned(sample_job.review_id)
            assert first.status == "UNCERTAIN" and "private-" not in first.model_dump_json()
            second = await ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            ).publish_owned(sample_job.review_id)
            assert second.status == "PUBLISHED" and second.comment_id == 41
            assert second.post_attempts == 1 and stub.post_calls == 1

    asyncio.run(run())


def test_missing_ambiguous_comment_never_causes_another_post(
    database, settings, sample_job, sample_context, sample_result
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        stub.behavior = "timeout_before"
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            publisher = ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            )
            for _ in range(5):
                assert (await publisher.publish_owned(sample_job.review_id)).status == "UNCERTAIN"
        assert stub.post_calls == 1

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["head", "base", "closed", "second_read"])
def test_stale_or_closed_pr_is_skipped_before_comment_write(
    database, settings, sample_job, sample_context, sample_result, kind
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        if kind == "head":
            stub.head = "c" * 40
        if kind == "base":
            stub.base = "c" * 40
        if kind == "closed":
            stub.state = "closed"
        if kind == "second_read":
            stub.change_on_second_read = True
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            result = await ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            ).publish_owned(sample_job.review_id)
        assert result.status == "SKIPPED" and stub.post_calls == 0

    asyncio.run(run())


@pytest.mark.parametrize("mode", [True, None])
def test_demo_and_unknown_legacy_modes_are_skipped_without_github_reads(
    database, settings, sample_job, sample_context, sample_result, mode
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result, mode=mode)
        stub = GitHubStub(sample_context)
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            result = await ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            ).publish_owned(sample_job.review_id)
        assert result.status == "SKIPPED" and not stub.calls

    asyncio.run(run())


def test_explicit_mock_publication_remains_labeled(
    database, settings, sample_job, sample_context, sample_result
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result, mode=True)
        stub = GitHubStub(sample_context)
        configured = publishing_settings(settings, github_publish_mock_reviews=True)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            result = await ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            ).publish_owned(sample_job.review_id)
        assert result.status == "PUBLISHED" and "Demo review" in stub.comments[0]["body"]

    asyncio.run(run())


def test_rate_rejection_can_retry_after_window_without_replaying_analysis(
    database, settings, sample_job, sample_context, sample_result
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        stub.behavior = "rate_once"
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            publisher = ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            )
            first = await publisher.publish_owned(sample_job.review_id)
            assert first.status == "PENDING" and first.next_retry_at is not None
            waiting = await publisher.publish_owned(sample_job.review_id)
            assert waiting.attempts == 1 and stub.post_calls == 1
            await publisher._save(
                sample_job.review_id, next_retry_at=datetime.now(UTC) - timedelta(seconds=1)
            )
            second = await publisher.publish_owned(sample_job.review_id)
            assert (
                second.status == "PUBLISHED"
                and second.post_attempts == 2
                and len(stub.comments) == 1
            )

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["forbidden", "history", "reads"])
def test_publishing_failures_have_their_own_bounded_status(
    database, settings, sample_job, sample_context, sample_result, problem
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        if problem == "forbidden":
            stub.behavior = "forbidden"
        if problem == "history":
            stub.full_history = False
        if problem == "reads":
            stub.get_failure = True
        configured = publishing_settings(settings, github_comment_max_pages=1)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            publisher = ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            )
            for _ in range(3):
                result = await publisher.publish_owned(sample_job.review_id)
                if result.status == "FAILED":
                    break
                await publisher._save(
                    sample_job.review_id, next_retry_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            assert result.status == "FAILED" and "private-" not in result.model_dump_json()
            assert stub.post_calls == (1 if problem == "forbidden" else 0)
            async with database.sessions() as session:
                assert (await session.get(Review, sample_job.review_id)).status == "COMPLETED"

    asyncio.run(run())


def test_cancellation_after_sending_is_committed_is_read_only_on_resume(
    database, settings, sample_job, sample_context, sample_result
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            github = GitHubService(client, configured)
            entered = asyncio.Event()
            original = github.post_review_comment

            async def blocked(*args):
                async with database.sessions() as session:
                    row = await session.get(ReviewPublication, sample_job.review_id)
                    assert row.status == "SENDING" and row.post_attempts == 1
                entered.set()
                await asyncio.Event().wait()

            github.post_review_comment = blocked
            publisher = ReviewPublisher(database.sessions, github, configured)
            task = asyncio.create_task(publisher.publish_owned(sample_job.review_id))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            github.post_review_comment = original
            assert (await publisher.publish_owned(sample_job.review_id)).status == "UNCERTAIN"
            assert stub.post_calls == 0

    asyncio.run(run())


@pytest.mark.parametrize("changed", ["author", "destination", "spoof"])
def test_reconciliation_checks_author_destination_and_exact_body(
    database, settings, sample_job, sample_context, sample_result, changed
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        stub.behavior = "timeout_after"
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            publisher = ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            )
            assert (await publisher.publish_owned(sample_job.review_id)).status == "UNCERTAIN"
            if changed == "author":
                stub.actor = 18
            if changed == "destination":
                publisher.destination_hash = "different-origin"
            if changed == "spoof":
                stub.comments[0]["user"]["id"] = 999
            result = await publisher.publish_owned(sample_job.review_id)
            assert result.status == "UNCERTAIN" and stub.post_calls == 1

    asyncio.run(run())


def test_preview_and_publication_read_apis_never_publish(
    database, settings, resources, sample_job, sample_context, sample_result
):
    asyncio.run(seed(database, sample_job, sample_context, sample_result))
    with TestClient(create_app(settings)) as client:
        client.app.state.session_factory = database.sessions
        preview = client.get(f"/reviews/{sample_job.review_id}/comment")
        assert preview.status_code == 200 and preview.headers["content-type"].startswith(
            "text/markdown"
        )
        assert "AI Pull Request Review" in preview.text and "system_prompt" not in preview.text
        result = client.get(f"/reviews/{sample_job.review_id}/publication")
        assert result.status_code == 200 and result.json()["status"] == "DISABLED"

    async def check():
        async with database.sessions() as session:
            assert await session.get(ReviewPublication, sample_job.review_id) is None

    asyncio.run(check())


def test_post_commit_before_record_update_recovers_without_duplicate(
    database, settings, sample_job, sample_context, sample_result, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        configured = publishing_settings(settings)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            publisher = ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            )
            monkeypatch.setattr(
                publisher,
                "_published",
                AsyncMock(
                    side_effect=OperationalError("simulated lost record update", None, Exception())
                ),
            )
            with pytest.raises(OperationalError):
                await publisher.publish_owned(sample_job.review_id)
            async with database.sessions() as session:
                assert (
                    await session.get(ReviewPublication, sample_job.review_id)
                ).status == "SENDING"
            rebuilt = ReviewPublisher(
                database.sessions, GitHubService(client, configured), configured
            )
            assert (await rebuilt.publish_owned(sample_job.review_id)).status == "PUBLISHED"
            assert stub.post_calls == 1

    asyncio.run(run())


def test_worker_publishes_after_persist_and_retries_without_repeating_reviewers(
    database, settings, multi_job, multi_context
):
    from contextlib import asynccontextmanager

    from fakeredis.aioredis import FakeRedis
    from langgraph.checkpoint.memory import InMemorySaver

    from app.services.job_queue import ReviewJobQueue
    from app.services.llm_service import MockLLMProvider
    from app.services.pr_cache import CachedGitHubService
    from app.services.worker_queue import DELAYED, WorkerQueue
    from app.worker.processor import WorkflowProcessor
    from app.worker.runner import ReviewWorker

    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            stub = GitHubStub(multi_context)
            stub.behavior = "rate_once"
            configured = publishing_settings(settings, github_publish_mock_reviews=True)
            async with build_github_client(configured, httpx.MockTransport(stub)) as client:
                github = GitHubService(client, configured)
                publisher = ReviewPublisher(database.sessions, github, configured)
                provider = MockLLMProvider()
                provider.generate = AsyncMock(wraps=provider.generate)
                saver = InMemorySaver()

                @asynccontextmanager
                async def checkpointers():
                    yield saver

                class SQLiteProcessor(WorkflowProcessor):
                    @asynccontextmanager
                    async def lock(self, job):
                        yield True

                processor_type = (
                    WorkflowProcessor if database.backend == "postgresql" else SQLiteProcessor
                )
                processor = processor_type(
                    database.engine,
                    database.sessions,
                    configured,
                    provider,
                    CachedGitHubService(github, redis, configured),
                    checkpointers=checkpointers,
                    publisher=publisher,
                )
                queue = WorkerQueue(redis, configured)
                worker = ReviewWorker(queue, processor, configured)
                await ReviewJobQueue(redis, 60).enqueue(multi_job)
                await worker.run_once()
                assert (await queue.status(multi_job.review_id)).status == "QUEUED"
                async with database.sessions() as session:
                    assert (await session.get(Review, multi_job.review_id)).status == "COMPLETED"
                await publisher._save(
                    multi_job.review_id, next_retry_at=datetime.now(UTC) - timedelta(seconds=1)
                )
                tokens = await redis.zrange(DELAYED, 0, -1)
                await redis.zadd(DELAYED, {token: 0 for token in tokens})
                await worker.run_once()
                status = await queue.status(multi_job.review_id)
                assert status.status == "COMPLETED" and status.attempts == 1
                assert provider.generate.await_count == 4 and stub.post_calls == 2
                assert len(stub.comments) == 1

    asyncio.run(run())


def test_publication_deadline_after_sending_does_not_become_a_safe_retry(
    database, settings, sample_job, sample_context, sample_result, monkeypatch
):
    async def run():
        await seed(database, sample_job, sample_context, sample_result)
        stub = GitHubStub(sample_context)
        configured = publishing_settings(settings)
        original_timeout = asyncio.timeout
        deadlines = []

        def capture_timeout(seconds):
            timer = original_timeout(seconds)
            if seconds == configured.github_publish_timeout_seconds:
                deadlines.append(timer)
            return timer

        monkeypatch.setattr("app.services.publication_service.asyncio.timeout", capture_timeout)
        async with build_github_client(configured, httpx.MockTransport(stub)) as client:
            github = GitHubService(client, configured)

            async def blocked(*args):
                # Expire the real deadline only once the durable SENDING boundary
                # is reached; DB startup speed must not decide this test's phase.
                deadlines[0].reschedule(asyncio.get_running_loop().time())
                await asyncio.Event().wait()

            github.post_review_comment = blocked
            result = await ReviewPublisher(database.sessions, github, configured).publish_owned(
                sample_job.review_id
            )
            assert result.status == "UNCERTAIN" and result.post_attempts == 1

    asyncio.run(run())
