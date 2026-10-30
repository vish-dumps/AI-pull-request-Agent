import asyncio
from collections.abc import Mapping
from pathlib import PurePosixPath

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import GitHubFileUnsupportedError
from app.db.context_repository import ContextRepository
from app.schemas.context import CodeChunk, IndexReport, repository_path
from app.schemas.github import CommitSHA, validate_repository
from app.services.embedding_service import (
    MAX_EMBEDDING_BYTES,
    EmbeddingProvider,
    validate_vectors,
)
from app.services.github_service import GitHubService

SUPPORTED_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".java",
    ".go",
    ".rs",
    ".md",
    ".rst",
}
EXCLUDED_PARTS = {
    "node_modules",
    ".git",
    "build",
    "dist",
    "generated",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    "coverage",
    ".next",
}


def selected_path(path: str) -> bool:
    try:
        repository_path(path)
    except ValueError:
        return False
    file = PurePosixPath(path.lower())
    return (
        file.suffix in SUPPORTED_SUFFIXES
        and not set(file.parts) & EXCLUDED_PARTS
        and not any(part.startswith(".") for part in file.parts)
        and not any(marker in file.name for marker in (".min.", ".generated.", "_pb2."))
    )


def chunk_file(path: str, content: str, max_chars: int) -> list[CodeChunk]:
    """Non-overlapping size chunks with original line anchors, even for long lines."""
    chunks = []
    offset, line = 0, 1
    while offset < len(content):
        end = min(offset + max_chars, len(content))
        while len(content[offset:end].encode("utf-8")) > MAX_EMBEDDING_BYTES:
            end -= 1
        # Prefer a line boundary if one fits, without dropping any source text.
        newline = content.rfind("\n", offset, end)
        if end < len(content) and newline >= offset:
            end = newline + 1
        text = content[offset:end]
        end_line = line + text.count("\n") - int(text.endswith("\n"))
        if text.strip():
            chunks.append(
                CodeChunk(
                    file_path=path, start_line=line, end_line=max(line, end_line), content=text
                )
            )
        line += text.count("\n")
        offset = end
    return chunks


class IndexingService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        provider: EmbeddingProvider,
        settings: Settings,
    ) -> None:
        self.sessions, self.provider, self.settings = sessions, provider, settings

    async def index_github(
        self, github: GitHubService, repository: str, ref: str | None = None
    ) -> IndexReport:
        async with asyncio.timeout(self.settings.index_timeout_seconds):
            commit_sha, tree_sha = await github.resolve_repository_commit(repository, ref)
            tree = await github.get_repository_tree(repository, tree_sha)
            files = {}
            warnings = ["GitHub's repository tree was truncated."] if tree.truncated else []
            total_bytes = 0
            fetched_files = 0
            selected = [
                entry
                for entry in sorted(tree.tree, key=lambda entry: entry.path)
                if entry.type == "blob"
                and entry.mode in {"100644", "100755"}
                and selected_path(entry.path)
            ]
            for entry in selected:
                if fetched_files >= self.settings.index_max_files:
                    warnings.append("Repository indexing reached the file budget.")
                    break
                if entry.size is None or entry.size > self.settings.index_max_file_bytes:
                    warnings.append("Oversized or unknown-size source files were skipped.")
                    continue
                if total_bytes + entry.size > self.settings.index_max_total_bytes:
                    warnings.append("Repository indexing reached the total byte budget.")
                    break
                try:
                    fetched_files += 1
                    content = await github.get_file_content(repository, entry.path, commit_sha)
                except GitHubFileUnsupportedError:
                    warnings.append("Unsupported source file content was skipped.")
                    continue
                actual_size = len(content.encode("utf-8"))
                if actual_size > self.settings.index_max_file_bytes:
                    warnings.append("Oversized source file content was skipped.")
                    continue
                if total_bytes + actual_size > self.settings.index_max_total_bytes:
                    warnings.append("Repository indexing reached the total byte budget.")
                    break
                total_bytes += actual_size
                files[entry.path] = content
            return await self._index_files(repository, commit_sha, files, warnings)

    async def index_files(
        self, repository: str, commit_sha: str, files: Mapping[str, str]
    ) -> IndexReport:
        """Normalized fixture/local input; caller guarantees the revision provenance."""
        async with asyncio.timeout(self.settings.index_timeout_seconds):
            return await self._index_files(repository, commit_sha, files, [])

    async def _index_files(
        self, repository: str, commit_sha: str, files: Mapping[str, str], warnings: list[str]
    ) -> IndexReport:
        validate_repository(repository)
        commit_sha = TypeAdapter(CommitSHA).validate_python(commit_sha).lower()
        chunks, file_count, total_bytes = [], 0, 0
        for path, content in sorted(files.items()):
            if not selected_path(path):
                continue
            if file_count >= self.settings.index_max_files:
                warnings.append("Repository indexing reached the file budget.")
                break
            size = len(content.encode("utf-8"))
            if size > self.settings.index_max_file_bytes or "\x00" in content:
                warnings.append("Oversized or binary source files were skipped.")
                continue
            if any(
                marker in content[:300].lower()
                for marker in (
                    "@generated",
                    "auto-generated",
                    "code generated",
                    "automatically generated",
                )
            ):
                continue
            if total_bytes + size > self.settings.index_max_total_bytes:
                warnings.append("Repository indexing reached the total byte budget.")
                break
            parts = chunk_file(path, content, self.settings.index_chunk_chars)
            remaining = self.settings.index_max_chunks - len(chunks)
            if len(parts) > remaining:
                warnings.append("Repository indexing reached the chunk budget.")
                chunks.extend(parts[:remaining])
                file_count += 1
                break
            chunks.extend(parts)
            file_count += 1
            total_bytes += size
        vectors = []
        batch_size = self.settings.embedding_batch_size
        for start in range(0, len(chunks), batch_size):
            texts = [chunk.content for chunk in chunks[start : start + batch_size]]
            result = await self.provider.embed(texts)
            vectors.extend(validate_vectors(result, len(texts), self.provider.dimensions))
        warnings = list(dict.fromkeys(warnings))[:10]
        # Generate everything before opening a transaction. Any failure preserves
        # the last committed snapshot, including deleted-file cleanup on replacement.
        async with self.sessions.begin() as session:
            return await ContextRepository(session).replace(
                repository,
                commit_sha,
                self.provider,
                chunks,
                vectors,
                file_count=file_count,
                limited=bool(warnings),
                warnings=warnings,
            )
