"""Add revision-scoped repository indexes and vector chunks.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.create_table(
        "repository_indexes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository", sa.String(140), nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("embedding_identity", sa.String(100), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("mock_mode", sa.Boolean(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("limited", sa.Boolean(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column(
            "indexed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_indexes")),
        sa.UniqueConstraint(
            "repository",
            "commit_sha",
            "embedding_identity",
            name=op.f("uq_repository_indexes_repository"),
        ),
        sa.CheckConstraint(
            "repository = lower(repository)",
            name=op.f("ck_repository_indexes_lowercase_repository"),
        ),
        sa.CheckConstraint(
            "embedding_dimensions > 0", name=op.f("ck_repository_indexes_positive_dimensions")
        ),
    )
    op.create_table(
        "repository_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("index_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_chunks")),
        sa.ForeignKeyConstraint(
            ["index_id"],
            ["repository_indexes.id"],
            ondelete="CASCADE",
            name=op.f("fk_repository_chunks_index_id_repository_indexes"),
        ),
        sa.UniqueConstraint("index_id", "ordinal", name=op.f("uq_repository_chunks_index_id")),
        sa.CheckConstraint(
            "start_line > 0 AND end_line >= start_line",
            name=op.f("ck_repository_chunks_line_range"),
        ),
    )
    op.create_index("ix_repository_chunks_index_id", "repository_chunks", ["index_id"])


def downgrade() -> None:
    op.drop_table("repository_chunks")
    op.drop_table("repository_indexes")
    # The vector extension can be shared by other schemas/applications; leave it installed.
