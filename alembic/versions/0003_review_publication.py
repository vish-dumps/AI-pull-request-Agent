"""Persist review mode and publication outbox. Revision 0003, revises 0002."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reviews", sa.Column("mock_mode", sa.Boolean(), nullable=True))
    op.create_table(
        "review_publications",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "SENDING",
                "PUBLISHED",
                "SKIPPED",
                "FAILED",
                "UNCERTAIN",
                name=op.f("ck_review_publications_publication_status"),
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("destination_hash", sa.String(64), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=True),
        sa.Column("comment_id", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("post_attempts", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("review_id", name=op.f("pk_review_publications")),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["reviews.id"],
            ondelete="CASCADE",
            name=op.f("fk_review_publications_review_id_reviews"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND post_attempts >= 0 AND post_attempts <= attempts",
            name=op.f("ck_review_publications_attempt_counts"),
        ),
        sa.CheckConstraint(
            "comment_id IS NULL OR comment_id > 0",
            name=op.f("ck_review_publications_positive_comment_id"),
        ),
        sa.CheckConstraint(
            "(status = 'PUBLISHED' AND comment_id IS NOT NULL AND published_at IS NOT NULL) OR "
            "(status <> 'PUBLISHED' AND comment_id IS NULL AND published_at IS NULL)",
            name=op.f("ck_review_publications_published_result"),
        ),
    )


def downgrade() -> None:
    op.drop_table("review_publications")
    op.drop_column("reviews", "mock_mode")
