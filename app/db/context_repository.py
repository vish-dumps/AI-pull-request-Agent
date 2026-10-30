from uuid import uuid4

from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RepositoryIndexError
from app.db.models import RepositoryChunk, RepositoryIndex
from app.schemas.context import CodeChunk, IndexReport, RetrievedChunk
from app.schemas.github import validate_repository
from app.services.embedding_service import EmbeddingProvider, validate_vectors


def index_report(row: RepositoryIndex) -> IndexReport:
    return IndexReport(
        index_id=row.id,
        repository=row.repository,
        commit_sha=row.commit_sha,
        embedding_identity=row.embedding_identity,
        embedding_dimensions=row.embedding_dimensions,
        mock_mode=row.mock_mode,
        file_count=row.file_count,
        chunk_count=row.chunk_count,
        limited=row.limited,
        warnings=row.warnings,
        indexed_at=row.indexed_at,
    )


class ContextRepository:
    """The caller owns short transactions; network I/O never takes place here."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def snapshot(
        self, repository: str, commit_sha: str, identity: str, *, lock: bool = False
    ) -> IndexReport | None:
        validate_repository(repository)
        statement = select(RepositoryIndex).where(
            RepositoryIndex.repository == repository.lower(),
            RepositoryIndex.commit_sha == commit_sha.lower(),
            RepositoryIndex.embedding_identity == identity,
        )
        if lock:
            statement = statement.with_for_update(read=True)
        row = await self.session.scalar(statement)
        return index_report(row) if row else None

    async def replace(
        self,
        repository: str,
        commit_sha: str,
        provider: EmbeddingProvider,
        chunks: list[CodeChunk],
        vectors: list[list[float]],
        *,
        file_count: int,
        limited: bool,
        warnings: list[str],
    ) -> IndexReport:
        validate_repository(repository)
        vectors = validate_vectors(vectors, len(chunks), provider.dimensions)
        values = dict(
            repository=repository.lower(),
            commit_sha=commit_sha.lower(),
            embedding_identity=provider.identity,
            embedding_dimensions=provider.dimensions,
            mock_mode=provider.mock_mode,
            file_count=file_count,
            chunk_count=len(chunks),
            limited=limited,
            warnings=warnings,
        )
        dialect = self.session.bind.dialect.name
        upsert = pg_insert if dialect == "postgresql" else sqlite_insert
        # Updating the snapshot obtains the row lock before replacing its children,
        # so concurrent writers publish one complete snapshot instead of mixed chunks.
        statement = upsert(RepositoryIndex).values(id=uuid4(), **values)
        statement = statement.on_conflict_do_update(
            index_elements=["repository", "commit_sha", "embedding_identity"],
            set_={**values, "indexed_at": func.now()},
        ).returning(RepositoryIndex.id)
        index_id = await self.session.scalar(statement)
        await self.session.execute(
            delete(RepositoryChunk).where(RepositoryChunk.index_id == index_id)
        )
        if chunks:
            await self.session.execute(
                insert(RepositoryChunk),
                [
                    {
                        "index_id": index_id,
                        "ordinal": ordinal,
                        **chunk.model_dump(),
                        "embedding": vector,
                    }
                    for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
                ],
            )
        row = await self.session.get(RepositoryIndex, index_id, populate_existing=True)
        return index_report(row)

    async def search(
        self,
        repository: str,
        commit_sha: str,
        identity: str,
        vector: list[float],
        *,
        exclude_paths: set[str],
        limit: int,
        min_similarity: float,
    ) -> list[RetrievedChunk]:
        if self.session.bind.dialect.name != "postgresql":
            raise RepositoryIndexError("Similarity retrieval requires PostgreSQL with PGVector")
        distance = RepositoryChunk.embedding.cosine_distance(vector)
        statement = (
            select(RepositoryChunk, distance.label("distance"))
            .join(RepositoryIndex, RepositoryChunk.index_id == RepositoryIndex.id)
            .where(
                RepositoryIndex.repository == repository.lower(),
                RepositoryIndex.commit_sha == commit_sha.lower(),
                RepositoryIndex.embedding_identity == identity,
                RepositoryIndex.embedding_dimensions == len(vector),
                RepositoryChunk.file_path.not_in(exclude_paths),
                distance <= 1 - min_similarity,
            )
            .order_by(distance, RepositoryChunk.file_path, RepositoryChunk.ordinal)
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).all()
        return [
            RetrievedChunk(
                repository=repository.lower(),
                commit_sha=commit_sha.lower(),
                similarity=max(-1.0, min(1.0, 1 - distance)),
                **{key: getattr(chunk, key) for key in CodeChunk.model_fields},
            )
            for chunk, distance in rows
        ]
