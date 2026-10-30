from datetime import datetime
from enum import Enum as PythonEnum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.schemas.publication import PublicationStatus
from app.schemas.review import AgentType, Recommendation, ReviewStatus, Severity


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def enum_column(enum: type[PythonEnum], name: str) -> Enum:
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("github_full_name = lower(github_full_name)", name="lowercase_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    github_full_name: Mapped[str] = mapped_column(String(140), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    pull_requests: Mapped[list["PullRequest"]] = relationship(
        back_populates="repository",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "pr_number"),
        CheckConstraint("pr_number > 0", name="positive_number"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    pr_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(100))
    base_branch: Mapped[str] = mapped_column(Text)
    head_branch: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    repository: Mapped[Repository] = relationship(back_populates="pull_requests", lazy="raise")
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="pull_request",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("risk_score IS NULL OR risk_score >= 0", name="nonnegative_risk_score"),
        CheckConstraint(
            "(status IN ('COMPLETED', 'PARTIAL', 'FAILED') AND completed_at IS NOT NULL) OR "
            "(status IN ('QUEUED', 'PROCESSING') AND completed_at IS NULL)",
            name="completion_timestamp",
        ),
        CheckConstraint(
            "(status IN ('COMPLETED', 'PARTIAL') AND risk IS NOT NULL AND risk_score IS NOT NULL "
            "AND summary IS NOT NULL AND recommendation IS NOT NULL) OR "
            "(status NOT IN ('COMPLETED', 'PARTIAL') AND risk IS NULL AND risk_score IS NULL "
            "AND summary IS NULL AND recommendation IS NULL)",
            name="result_matches_status",
        ),
        CheckConstraint(
            "(status = 'FAILED' AND error_message IS NOT NULL) OR "
            "(status <> 'FAILED' AND error_message IS NULL)",
            name="error_matches_status",
        ),
        Index("ix_reviews_pull_request_created", "pull_request_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    delivery_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    pull_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE")
    )
    status: Mapped[ReviewStatus] = mapped_column(enum_column(ReviewStatus, "review_status"))
    base_sha: Mapped[str] = mapped_column(String(40))
    head_sha: Mapped[str] = mapped_column(String(40))
    mock_mode: Mapped[bool | None] = mapped_column(Boolean)
    risk: Mapped[Severity | None] = mapped_column(enum_column(Severity, "risk_level"))
    risk_score: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    recommendation: Mapped[Recommendation | None] = mapped_column(
        enum_column(Recommendation, "recommendation")
    )
    error_message: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pull_request: Mapped[PullRequest] = relationship(back_populates="reviews", lazy="raise")
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="review",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
        order_by="Finding.id",
    )


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("line_number IS NULL OR line_number > 0", name="positive_line"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    review_id: Mapped[UUID] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )
    agent_type: Mapped[AgentType] = mapped_column(enum_column(AgentType, "agent_type"))
    file_path: Mapped[str] = mapped_column(Text)
    line_number: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, "severity"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    review: Mapped[Review] = relationship(back_populates="findings", lazy="raise")


class RepositoryIndex(Base):
    __tablename__ = "repository_indexes"
    __table_args__ = (
        UniqueConstraint("repository", "commit_sha", "embedding_identity"),
        CheckConstraint("repository = lower(repository)", name="lowercase_repository"),
        CheckConstraint("embedding_dimensions > 0", name="positive_dimensions"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository: Mapped[str] = mapped_column(String(140))
    commit_sha: Mapped[str] = mapped_column(String(40))
    embedding_identity: Mapped[str] = mapped_column(String(100))
    embedding_dimensions: Mapped[int] = mapped_column(Integer)
    mock_mode: Mapped[bool] = mapped_column(Boolean)
    file_count: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer)
    limited: Mapped[bool] = mapped_column(Boolean)
    warnings: Mapped[list[str]] = mapped_column(JSON)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepositoryChunk(Base):
    __tablename__ = "repository_chunks"
    __table_args__ = (
        UniqueConstraint("index_id", "ordinal"),
        CheckConstraint("start_line > 0 AND end_line >= start_line", name="line_range"),
        Index("ix_repository_chunks_index_id", "index_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    index_id: Mapped[UUID] = mapped_column(ForeignKey("repository_indexes.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    # Variable dimensions permit explicit, isolated embedding profiles without DDL changes.
    # JSON supports storage/migration tests on SQLite; similarity requires PostgreSQL.
    embedding: Mapped[list[float]] = mapped_column(Vector().with_variant(JSON(), "sqlite"))


class ReviewPublication(Base):
    __tablename__ = "review_publications"
    __table_args__ = (
        CheckConstraint(
            "attempts >= 0 AND post_attempts >= 0 AND post_attempts <= attempts",
            name="attempt_counts",
        ),
        CheckConstraint("comment_id IS NULL OR comment_id > 0", name="positive_comment_id"),
        CheckConstraint(
            "(status = 'PUBLISHED' AND comment_id IS NOT NULL AND published_at IS NOT NULL) OR "
            "(status <> 'PUBLISHED' AND comment_id IS NULL AND published_at IS NULL)",
            name="published_result",
        ),
    )
    review_id: Mapped[UUID] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[PublicationStatus] = mapped_column(
        enum_column(PublicationStatus, "publication_status")
    )
    body: Mapped[str | None] = mapped_column(Text)
    destination_hash: Mapped[str] = mapped_column(String(64))
    author_id: Mapped[int | None] = mapped_column(BigInteger)
    comment_id: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    post_attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(String(500))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
