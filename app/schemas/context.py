from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.github import CommitSHA, RepositoryName


def repository_path(value: str) -> str:
    if (
        not value
        or len(value) > 1024
        or "\\" in value
        or any(ord(char) < 32 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Expected a repository-relative file path")
    return value


class IndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: RepositoryName
    ref: str | None = Field(default=None, min_length=1, max_length=200)


class CodeChunk(BaseModel):
    file_path: str
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    content: str = Field(min_length=1, max_length=6000)

    _path = field_validator("file_path")(repository_path)


class RetrievedChunk(CodeChunk):
    repository: RepositoryName
    commit_sha: CommitSHA
    similarity: float = Field(ge=-1, le=1)


class IndexReport(BaseModel):
    index_id: UUID
    repository: RepositoryName
    commit_sha: CommitSHA
    embedding_identity: str
    embedding_dimensions: int
    mock_mode: bool
    file_count: int
    chunk_count: int
    limited: bool
    warnings: list[str]
    indexed_at: datetime


class RetrievedContext(BaseModel):
    source: Literal["diff_only", "pgvector"] = "diff_only"
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
