import asyncio
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ReviewConflictError, ReviewStateError
from app.db.models import Base, Finding, PullRequest, Repository, Review
from app.db.repositories import ReviewRepository
from app.schemas.review import ReviewStatus


async def counts(sessions):
    async with sessions() as session:
        return [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Repository, PullRequest, Review, Finding)
        ]


def test_migrations_match_models_and_round_trip(database):
    def check(connection):
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "repositories",
            "pull_requests",
            "reviews",
            "findings",
        }
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []

    async def run():
        async with database.engine.begin() as connection:
            await connection.run_sync(check)

    asyncio.run(run())


def test_review_and_findings_persist_across_sessions(
    database, sample_job, sample_context, sample_result
):
    async def run():
        async with database.sessions.begin() as session:
            store = ReviewRepository(session)
            await store.create_review(sample_job, sample_context)
            await store.mark_processing(sample_job.review_id)
            await store.complete_review(sample_job.review_id, sample_result)
        async with database.sessions() as session:
            review = await ReviewRepository(session).get_review(sample_job.review_id)
            assert review.status == ReviewStatus.COMPLETED
            assert review.head_sha == sample_context.head_sha
            assert review.pull_request.repository.github_full_name == "octocat/hello-world"
            assert review.risk_score == 4.5
            assert review.completed_at is not None
            assert len(review.findings) == 1
            assert review.findings[0].confidence == 0.9
        assert await counts(database.sessions) == [1, 1, 1, 1]

    asyncio.run(run())


def test_creation_is_idempotent_and_case_insensitive(database, sample_job, sample_context):
    async def run():
        for name in ("Octocat/HELLO-world", "octocat/hello-world"):
            async with database.sessions.begin() as session:
                store = ReviewRepository(session)
                job = sample_job.model_copy(update={"repository": name})
                await store.create_review(job, sample_context)
        assert await counts(database.sessions) == [1, 1, 1, 0]

    asyncio.run(run())


def test_new_revision_reuses_pr_but_preserves_review_snapshot(database, sample_job, sample_context):
    async def run():
        async with database.sessions.begin() as session:
            await ReviewRepository(session).create_review(sample_job, sample_context)
        second_job = sample_job.model_copy(
            update={"review_id": uuid4(), "delivery_id": uuid4(), "head_sha": "c" * 40}
        )
        second_context = sample_context.model_copy(
            update={"head_sha": "c" * 40, "title": "New title"}
        )
        async with database.sessions.begin() as session:
            await ReviewRepository(session).create_review(second_job, second_context)
        assert await counts(database.sessions) == [1, 1, 2, 0]
        async with database.sessions() as session:
            store = ReviewRepository(session)
            first = await store.get_review(sample_job.review_id)
            second = await store.get_review(second_job.review_id)
            assert first.pull_request_id == second.pull_request_id
            assert first.head_sha == "b" * 40
            assert second.head_sha == "c" * 40

    asyncio.run(run())


def test_caller_rollback_removes_entire_result(database, sample_job, sample_context, sample_result):
    async def run():
        with pytest.raises(RuntimeError):
            async with database.sessions.begin() as session:
                store = ReviewRepository(session)
                await store.create_review(sample_job, sample_context)
                await store.mark_processing(sample_job.review_id)
                await store.complete_review(sample_job.review_id, sample_result)
                raise RuntimeError("Simulated failure before commit")
        assert await counts(database.sessions) == [0, 0, 0, 0]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["repository", "pr_number", "head_sha", "base_sha"])
def test_mismatched_job_context_is_rejected(database, sample_job, sample_context, change):
    value = {
        "repository": "other/repo",
        "pr_number": 99,
        "head_sha": "c" * 40,
        "base_sha": "d" * 40,
    }

    async def run():
        with pytest.raises(ReviewConflictError):
            async with database.sessions.begin() as session:
                await ReviewRepository(session).create_review(
                    sample_job.model_copy(update={change: value[change]}), sample_context
                )
        assert await counts(database.sessions) == [0, 0, 0, 0]

    asyncio.run(run())


def test_reused_review_id_with_different_delivery_is_rejected(database, sample_job, sample_context):
    async def run():
        async with database.sessions.begin() as session:
            await ReviewRepository(session).create_review(sample_job, sample_context)
        with pytest.raises(ReviewConflictError):
            async with database.sessions.begin() as session:
                await ReviewRepository(session).create_review(
                    sample_job.model_copy(update={"delivery_id": uuid4()}), sample_context
                )
        assert await counts(database.sessions) == [1, 1, 1, 0]

    asyncio.run(run())


def test_delivery_cannot_create_two_reviews(database, sample_job, sample_context):
    async def run():
        async with database.sessions.begin() as session:
            await ReviewRepository(session).create_review(sample_job, sample_context)
        with pytest.raises(ReviewConflictError):
            async with database.sessions.begin() as session:
                await ReviewRepository(session).create_review(
                    sample_job.model_copy(update={"review_id": uuid4()}), sample_context
                )
        assert await counts(database.sessions) == [1, 1, 1, 0]

    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["COMPLETED", "PARTIAL", "FAILED"])
def test_terminal_reviews_cannot_be_restarted(
    database, sample_job, sample_context, sample_result, terminal
):
    async def run():
        async with database.sessions.begin() as session:
            store = ReviewRepository(session)
            await store.create_review(sample_job, sample_context)
            if terminal == "FAILED":
                await store.fail_review(sample_job.review_id, "Synthetic failure")
            else:
                await store.mark_processing(sample_job.review_id)
                await store.complete_review(
                    sample_job.review_id,
                    sample_result.model_copy(update={"status": ReviewStatus(terminal)}),
                )
        with pytest.raises(ReviewStateError):
            async with database.sessions.begin() as session:
                await ReviewRepository(session).mark_processing(sample_job.review_id)
        async with database.sessions() as session:
            review = await ReviewRepository(session).get_review(sample_job.review_id)
            assert review.status == terminal
            assert review.completed_at is not None

    asyncio.run(run())


def test_queued_review_cannot_complete_without_processing(
    database, sample_job, sample_context, sample_result
):
    async def run():
        async with database.sessions.begin() as session:
            store = ReviewRepository(session)
            await store.create_review(sample_job, sample_context)
            with pytest.raises(ReviewStateError):
                await store.complete_review(sample_job.review_id, sample_result)

    asyncio.run(run())


def test_database_rejects_invalid_confidence(database, sample_job, sample_context, sample_result):
    async def run():
        async with database.sessions.begin() as session:
            store = ReviewRepository(session)
            await store.create_review(sample_job, sample_context)
            await store.mark_processing(sample_job.review_id)
            await store.complete_review(sample_job.review_id, sample_result)
        with pytest.raises(IntegrityError):
            async with database.sessions.begin() as session:
                await session.execute(update(Finding).values(confidence=1.1))

    asyncio.run(run())


def test_database_rejects_incomplete_terminal_result(database, sample_job, sample_context):
    async def run():
        async with database.sessions.begin() as session:
            await ReviewRepository(session).create_review(sample_job, sample_context)
        with pytest.raises(IntegrityError):
            async with database.sessions.begin() as session:
                await session.execute(text("UPDATE reviews SET status = 'COMPLETED'"))

    asyncio.run(run())


def test_foreign_key_cascades_remove_children(database, sample_job, sample_context, sample_result):
    async def run():
        async with database.sessions.begin() as session:
            store = ReviewRepository(session)
            await store.create_review(sample_job, sample_context)
            await store.mark_processing(sample_job.review_id)
            await store.complete_review(sample_job.review_id, sample_result)
        async with database.sessions.begin() as session:
            await session.execute(delete(Repository))
        assert await counts(database.sessions) == [0, 0, 0, 0]

    asyncio.run(run())


def test_concurrent_duplicate_ingestion_on_postgres(database, sample_job, sample_context):
    if database.backend != "postgresql":
        pytest.skip("Row-level concurrency is verified against PostgreSQL")

    async def run():
        async def ingest():
            async with database.sessions.begin() as session:
                review = await ReviewRepository(session).create_review(sample_job, sample_context)
                return review.id

        ids = await asyncio.gather(*(ingest() for _ in range(4)))
        assert ids == [sample_job.review_id] * 4
        assert await counts(database.sessions) == [1, 1, 1, 0]

    asyncio.run(run())
