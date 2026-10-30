import asyncio
import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.context_repository import ContextRepository
from app.schemas.context import RetrievedContext
from app.schemas.github import PullRequestContext
from app.services.embedding_service import MAX_EMBEDDING_BYTES, EmbeddingProvider, validate_vectors


class ContextService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        provider: EmbeddingProvider,
        settings: Settings,
    ) -> None:
        self.sessions, self.provider, self.settings = sessions, provider, settings
        profile = [
            provider.identity,
            settings.max_context_chunks,
            settings.max_context_chars,
            settings.retrieval_query_chars,
            settings.retrieval_min_similarity,
        ]
        self.identity = hashlib.sha256(json.dumps(profile).encode()).hexdigest()

    async def retrieve(self, context: PullRequestContext) -> RetrievedContext:
        async with asyncio.timeout(self.settings.retrieval_timeout_seconds):
            return await self._retrieve(context)

    async def _retrieve(self, context: PullRequestContext) -> RetrievedContext:
        async with self.sessions() as session:
            snapshot = await ContextRepository(session).snapshot(
                context.repository, context.base_sha, self.provider.identity
            )
        if snapshot is None:
            return RetrievedContext(
                warnings=[
                    "No matching repository index for this base commit and embedding profile."
                ]
            )
        if snapshot.embedding_dimensions != self.provider.dimensions:
            return RetrievedContext(warnings=["Repository index dimensions do not match."])
        # Only bounded changed code is embedded; PR prose cannot steer retrieval.
        query = "\n".join(
            line[1:]
            for file in context.changed_files
            for line in (file.patch or "").splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )[: self.settings.retrieval_query_chars]
        query = query.encode("utf-8")[:MAX_EMBEDDING_BYTES].decode("utf-8", errors="ignore")
        if not query.strip():
            return RetrievedContext(warnings=["No added code was available for context retrieval."])
        vector = validate_vectors(await self.provider.embed([query]), 1, self.provider.dimensions)[
            0
        ]
        exclude = {file.filename for file in context.changed_files}
        exclude.update(
            file.previous_filename for file in context.changed_files if file.previous_filename
        )
        async with self.sessions.begin() as session:
            # Keep coverage metadata and snippets consistent during a reindex.
            # The shared lock is held only for database reads, never embedding I/O.
            snapshot = await ContextRepository(session).snapshot(
                context.repository, context.base_sha, self.provider.identity, lock=True
            )
            if snapshot is None or snapshot.embedding_dimensions != self.provider.dimensions:
                return RetrievedContext(
                    warnings=["Repository index changed or became unavailable during retrieval."]
                )
            matches = await ContextRepository(session).search(
                context.repository,
                context.base_sha,
                self.provider.identity,
                vector,
                exclude_paths=exclude,
                limit=self.settings.max_context_chunks,
                min_similarity=self.settings.retrieval_min_similarity,
            )
        chunks = []
        remaining = self.settings.max_context_chars
        for match in matches:
            if remaining <= 0:
                break
            text = match.content[:remaining]
            if len(text) < len(match.content) and "\n" in text:
                text = text.rsplit("\n", 1)[0]
            if not text.strip():
                continue
            end_line = match.start_line + text.count("\n") - int(text.endswith("\n"))
            chunks.append(
                match.model_copy(
                    update={"content": text, "end_line": max(match.start_line, end_line)}
                )
            )
            remaining -= len(text)
        return RetrievedContext(
            source="pgvector",
            chunks=chunks,
            warnings=["Repository index was limited during indexing."] if snapshot.limited else [],
        )
