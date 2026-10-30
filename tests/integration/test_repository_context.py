import asyncio
import base64
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.agents.factory import create_reviewers
from app.api.routes.repositories import indexing_services
from app.core.exceptions import EmbeddingError
from app.db.context_repository import ContextRepository
from app.db.models import RepositoryChunk, RepositoryIndex
from app.graph.workflow import ReviewWorkflow
from app.main import create_app
from app.services.context_service import ContextService
from app.services.embedding_service import MockEmbeddingProvider
from app.services.github_service import GitHubService, build_github_client
from app.services.indexing_service import IndexingService
from app.services.llm_service import MockLLMProvider

SOURCE = {
    "src/related.py": "def load_user(user_id):\n    return validate_user_id(user_id)\n",
    "tests/test_user.py": "def test_load_user():\n    assert load_user('valid-user-id')\n",
}


def test_index_replace_removes_deleted_chunks_and_is_revision_scoped(database, settings):
    async def run():
        provider = MockEmbeddingProvider(8)
        indexer = IndexingService(database.sessions, provider, settings)
        first = await indexer.index_files("Demo/Repo", "a" * 40, SOURCE)
        second = await indexer.index_files(
            "demo/repo", "a" * 40, {"src/new.py": "def new(): return 1"}
        )
        assert first.index_id == second.index_id
        assert second.chunk_count == 1
        await indexer.index_files("demo/repo", "b" * 40, SOURCE)
        await IndexingService(database.sessions, MockEmbeddingProvider(16), settings).index_files(
            "demo/repo", "a" * 40, SOURCE
        )
        await indexer.index_files("another/repo", "a" * 40, SOURCE)
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(RepositoryIndex)) == 4
            paths = (
                await session.scalars(
                    select(RepositoryChunk.file_path).where(
                        RepositoryChunk.index_id == second.index_id
                    )
                )
            ).all()
            assert paths == ["src/new.py"]

    asyncio.run(run())


def test_embedding_failure_does_not_replace_existing_snapshot(database, settings):
    async def run():
        provider = MockEmbeddingProvider()
        indexer = IndexingService(database.sessions, provider, settings)
        first = await indexer.index_files("demo/repo", "a" * 40, SOURCE)
        provider.embed = AsyncMock(side_effect=EmbeddingError("safe error"))
        with pytest.raises(EmbeddingError):
            await indexer.index_files("demo/repo", "a" * 40, {"new.py": "new source"})
        async with database.sessions() as session:
            current = await ContextRepository(session).snapshot(
                "demo/repo", "a" * 40, provider.identity
            )
            assert current.index_id == first.index_id
            assert current.chunk_count == 2
            assert set((await session.scalars(select(RepositoryChunk.file_path))).all()) == set(
                SOURCE
            )

    asyncio.run(run())


def test_chunk_write_failure_rolls_back_snapshot_and_deletions(database, settings):
    async def run():
        indexer = IndexingService(database.sessions, MockEmbeddingProvider(), settings)
        first = await indexer.index_files("demo/repo", "a" * 40, SOURCE)

        def fail_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO repository_chunks"):
                raise OperationalError("simulated chunk write failure", None, Exception())

        event.listen(database.engine.sync_engine, "before_cursor_execute", fail_insert)
        try:
            with pytest.raises(OperationalError):
                await indexer.index_files("demo/repo", "a" * 40, {"new.py": "new source"})
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", fail_insert)
        async with database.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(RepositoryChunk))
                == first.chunk_count
            )
            assert set((await session.scalars(select(RepositoryChunk.file_path))).all()) == set(
                SOURCE
            )

    asyncio.run(run())


def test_indexing_budgets_generated_binary_and_empty_replacement(database, settings):
    async def run():
        configured = settings.model_copy(
            update={
                "index_max_files": 1,
                "index_chunk_chars": 128,
                "index_max_chunks": 1,
                "embedding_batch_size": 1,
            }
        )
        provider = MockEmbeddingProvider()
        provider.embed = AsyncMock(wraps=provider.embed)
        indexer = IndexingService(database.sessions, provider, configured)
        report = await indexer.index_files(
            "demo/repo",
            "a" * 40,
            {
                "src/0.py": "@generated\nignored",
                "src/1.py": "\x00binary",
                "src/2.py": "def a():\n" + "x" * 300,
                "src/3.py": "def b(): pass",
                "node_modules/a.py": "ignored",
            },
        )
        assert report.chunk_count == 1 and report.file_count == 1 and report.limited
        assert provider.embed.await_count == 1
        empty = await indexer.index_files("demo/repo", "a" * 40, {})
        assert empty.index_id == report.index_id and empty.chunk_count == 0
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(RepositoryChunk)) == 0

    asyncio.run(run())


def github_handler(requests, *, truncated=False, broken=False):
    sha, tree_sha = "a" * 40, "d" * 40
    files = {
        "src/related.py": SOURCE["src/related.py"],
        "node_modules/a.js": "skip",
        "src/symlink.py": "skip",
        "image.png": "skip",
    }

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/repos/demo/repo":
            return httpx.Response(200, json={"full_name": "demo/repo", "default_branch": "main"})
        if "/commits/" in path:
            return httpx.Response(200, json={"sha": sha, "commit": {"tree": {"sha": tree_sha}}})
        if "/git/trees/" in path:
            assert request.url.params["recursive"] == "1"
            return httpx.Response(
                200,
                json={
                    "sha": tree_sha,
                    "truncated": truncated,
                    "tree": [
                        {
                            "path": name,
                            "mode": "120000" if "symlink" in name else "100644",
                            "type": "blob",
                            "sha": "e" * 40,
                            "size": len(content),
                        }
                        for name, content in files.items()
                    ],
                },
            )
        assert request.url.params["ref"] == sha
        if broken:
            return httpx.Response(403, json={"message": "private-message"})
        content = files[path.split("/contents/", 1)[1]]
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": len(content),
                "content": base64.b64encode(content.encode()).decode(),
            },
        )

    return handler


def test_github_index_pins_commit_and_skips_symlinks_dependencies(database, settings):
    requests = []

    async def run():
        async with build_github_client(
            settings, httpx.MockTransport(github_handler(requests, truncated=True))
        ) as client:
            report = await IndexingService(
                database.sessions, MockEmbeddingProvider(), settings
            ).index_github(GitHubService(client, settings), "demo/repo")
        assert report.commit_sha == "a" * 40
        assert report.file_count == report.chunk_count == 1
        assert report.limited and "truncated" in report.warnings[0]

    asyncio.run(run())
    assert len(requests) == 4
    assert sum("/contents/" in request.url.path for request in requests) == 1


def test_index_http_endpoint_persists_source_and_safe_failure(database, settings, resources):
    requests = []
    app = create_app(settings)

    async def services():
        async with build_github_client(
            settings, httpx.MockTransport(github_handler(requests))
        ) as client:
            yield (
                IndexingService(database.sessions, MockEmbeddingProvider(), settings),
                GitHubService(client, settings),
            )

    app.dependency_overrides[indexing_services] = services
    with TestClient(app) as client:
        response = client.post("/repositories/index", json={"repository": "demo/repo"})
        assert response.status_code == 200
        assert response.json()["chunk_count"] == 1 and response.json()["mock_mode"]
        assert "content" not in response.json() and "embedding" not in response.json()
        assert (
            client.post("/repositories/index", json={"repository": "../escape"}).status_code == 422
        )
        assert (
            client.post(
                "/repositories/index", json={"repository": "demo/repo", "url": "https://evil"}
            ).status_code
            == 422
        )

    async def failing_services():
        async with build_github_client(
            settings, httpx.MockTransport(github_handler([], broken=True))
        ) as client:
            yield (
                IndexingService(database.sessions, MockEmbeddingProvider(), settings),
                GitHubService(client, settings),
            )

    app.dependency_overrides[indexing_services] = failing_services
    with TestClient(app) as client:
        response = client.post("/repositories/index", json={"repository": "demo/repo"})
        assert response.status_code == 502
        assert "private-message" not in response.text


def test_pgvector_similarity_filters_revision_repository_profile_and_changed_paths(
    database, settings, multi_context
):
    if database.backend != "postgresql":
        pytest.skip("Cosine similarity is verified using actual PGVector")

    async def run():
        provider = MockEmbeddingProvider(128)
        configured = settings.model_copy(
            update={
                "max_context_chunks": 2,
                "max_context_chars": 128,
                "retrieval_min_similarity": 0.0,
            }
        )
        indexer = IndexingService(database.sessions, provider, configured)
        source = {
            **SOURCE,
            "src/users.py": "SQL username query user users load_user user_id",
            "src/unrelated.py": "def paint_pixel(): return 'red'",
        }
        await indexer.index_files(multi_context.repository, multi_context.base_sha, source)
        await indexer.index_files("other/repo", multi_context.base_sha, {"leak.py": "user_id"})
        await indexer.index_files(multi_context.repository, "b" * 40, {"future.py": "user_id"})
        await IndexingService(database.sessions, MockEmbeddingProvider(64), configured).index_files(
            multi_context.repository, multi_context.base_sha, {"wrong_profile.py": "user_id"}
        )
        result = await ContextService(database.sessions, provider, configured).retrieve(
            multi_context
        )
        assert result.source == "pgvector" and 1 <= len(result.chunks) <= 2
        assert sum(len(chunk.content) for chunk in result.chunks) <= 128
        assert all(
            chunk.file_path in {"src/related.py", "tests/test_user.py", "src/unrelated.py"}
            for chunk in result.chunks
        )
        assert all(chunk.commit_sha == multi_context.base_sha for chunk in result.chunks)
        assert all(chunk.repository == multi_context.repository for chunk in result.chunks)
        async with database.sessions() as session:
            vector = (await provider.embed([SOURCE["src/related.py"]]))[0]
            exact = await ContextRepository(session).search(
                multi_context.repository,
                multi_context.base_sha,
                provider.identity,
                vector,
                exclude_paths=set(),
                limit=1,
                min_similarity=0.999,
            )
            assert exact[0].file_path == "src/related.py" and exact[0].similarity > 0.999

    asyncio.run(run())


def test_missing_index_avoids_embedding_calls_and_marks_partial(
    database, settings, multi_context, multi_job
):
    async def run():
        embeddings = MockEmbeddingProvider()
        embeddings.embed = AsyncMock(side_effect=AssertionError("No index means no paid query"))
        workflow = ReviewWorkflow(
            reviewers=create_reviewers(MockLLMProvider(), settings),
            mock_mode=True,
            context_service=ContextService(database.sessions, embeddings, settings),
        )
        await workflow.start(multi_job, multi_context)
        report = await workflow.report(multi_job.review_id)
        assert report.status == "PARTIAL" and report.context_source == "diff_only"
        assert "No matching repository index" in report.context_warnings[0]
        assert len(report.review.findings) == 4
        embeddings.embed.assert_not_awaited()

    asyncio.run(run())


def test_pgvector_context_reaches_all_reviewers(database, settings, multi_context, multi_job):
    if database.backend != "postgresql":
        pytest.skip("Context injection integration requires actual PGVector")
    import json

    async def run():
        embeddings = MockEmbeddingProvider()
        await IndexingService(database.sessions, embeddings, settings).index_files(
            multi_context.repository, multi_context.base_sha, SOURCE
        )
        provider = MockLLMProvider()
        provider.generate = AsyncMock(wraps=provider.generate)
        workflow = ReviewWorkflow(
            reviewers=create_reviewers(provider, settings),
            mock_mode=True,
            sessions=database.sessions,
            context_service=ContextService(database.sessions, embeddings, settings),
        )
        await workflow.start(multi_job, multi_context)
        report = await workflow.report(multi_job.review_id)
        assert report.status == "COMPLETED" and report.persisted
        assert report.context_source == "pgvector" and report.retrieved_context
        assert len(provider.generate.await_args_list) == 4
        for call in provider.generate.await_args_list:
            assert json.loads(call.args[0].user_prompt)["retrieved_context"]

    asyncio.run(run())


def test_pgvector_index_deadline_leaves_old_snapshot_intact(database, settings):
    async def run():
        configured = settings.model_copy(update={"index_timeout_seconds": 0.02})
        provider = MockEmbeddingProvider()
        indexer = IndexingService(database.sessions, provider, settings)
        original = await indexer.index_files("demo/repo", "a" * 40, SOURCE)

        async def blocked(texts):
            await asyncio.Event().wait()

        provider.embed = blocked
        with pytest.raises(TimeoutError):
            await IndexingService(database.sessions, provider, configured).index_files(
                "demo/repo", "a" * 40, {"new.py": "new source"}
            )
        async with database.sessions() as session:
            current = await ContextRepository(session).snapshot(
                "demo/repo", "a" * 40, provider.identity
            )
            assert current.index_id == original.index_id and current.chunk_count == 2

    asyncio.run(run())


def test_concurrent_index_replacement_publishes_complete_snapshot(database, settings):
    if database.backend != "postgresql":
        pytest.skip("Concurrent snapshot writers require PostgreSQL row locks")

    async def run():
        indexer = IndexingService(database.sessions, MockEmbeddingProvider(), settings)
        await asyncio.gather(
            indexer.index_files("demo/repo", "a" * 40, {"first.py": "first source"}),
            indexer.index_files("demo/repo", "a" * 40, SOURCE),
        )
        async with database.sessions() as session:
            snapshot = await ContextRepository(session).snapshot(
                "demo/repo", "a" * 40, indexer.provider.identity
            )
            paths = set((await session.scalars(select(RepositoryChunk.file_path))).all())
            assert paths in ({"first.py"}, set(SOURCE))
            assert snapshot.chunk_count == len(paths)
            assert await session.scalar(select(func.count()).select_from(RepositoryIndex)) == 1

    asyncio.run(run())


def test_pgvector_retrieval_timeout_preserves_diff_review(
    database, settings, multi_context, multi_job
):
    if database.backend != "postgresql":
        pytest.skip("Retrieval deadline integration requires PGVector")

    async def run():
        provider = MockEmbeddingProvider()
        await IndexingService(database.sessions, provider, settings).index_files(
            multi_context.repository, multi_context.base_sha, SOURCE
        )

        async def blocked(texts):
            await asyncio.Event().wait()

        provider.embed = blocked
        configured = settings.model_copy(update={"retrieval_timeout_seconds": 0.02})
        workflow = ReviewWorkflow(
            reviewers=create_reviewers(MockLLMProvider(), settings),
            mock_mode=True,
            context_service=ContextService(database.sessions, provider, configured),
        )
        await workflow.start(multi_job, multi_context)
        report = await workflow.report(multi_job.review_id)
        assert report.status == "PARTIAL" and len(report.review.findings) == 4
        assert "unavailable" in report.context_warnings[0]

    asyncio.run(run())


def test_retrieval_uses_current_coverage_after_concurrent_reindex(
    database, settings, multi_context
):
    if database.backend != "postgresql":
        pytest.skip("Atomic context snapshot reads require PostgreSQL/PGVector")

    async def run():
        writer = MockEmbeddingProvider()
        await IndexingService(database.sessions, writer, settings).index_files(
            multi_context.repository, multi_context.base_sha, SOURCE
        )
        query_provider = MockEmbeddingProvider()
        original_embed = query_provider.embed

        async def embed_and_replace(texts):
            result = await original_embed(texts)
            configured = settings.model_copy(update={"index_max_files": 1})
            await IndexingService(database.sessions, writer, configured).index_files(
                multi_context.repository, multi_context.base_sha, SOURCE
            )
            return result

        query_provider.embed = embed_and_replace
        result = await ContextService(database.sessions, query_provider, settings).retrieve(
            multi_context
        )
        assert result.source == "pgvector"
        assert "limited" in result.warnings[0]

    asyncio.run(run())
