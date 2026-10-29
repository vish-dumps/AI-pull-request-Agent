"""Create repositories, pull requests, reviews, and findings.

Revision ID: 0001
Revises: None
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("github_full_name", sa.String(140), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
        sa.UniqueConstraint("github_full_name", name=op.f("uq_repositories_github_full_name")),
        sa.CheckConstraint(
            "github_full_name = lower(github_full_name)",
            name=op.f("ck_repositories_lowercase_name"),
        ),
    )
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.String(100), nullable=False),
        sa.Column("base_branch", sa.Text(), nullable=False),
        sa.Column("head_branch", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pull_requests")),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            ondelete="CASCADE",
            name=op.f("fk_pull_requests_repository_id_repositories"),
        ),
        sa.UniqueConstraint(
            "repository_id", "pr_number", name=op.f("uq_pull_requests_repository_id")
        ),
        sa.CheckConstraint("pr_number > 0", name=op.f("ck_pull_requests_positive_number")),
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("pull_request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "PROCESSING",
                "COMPLETED",
                "PARTIAL",
                "FAILED",
                name=op.f("ck_reviews_review_status"),
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("base_sha", sa.String(40), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column(
            "risk",
            sa.Enum(
                "LOW",
                "MEDIUM",
                "HIGH",
                "CRITICAL",
                name=op.f("ck_reviews_risk_level"),
                native_enum=False,
                create_constraint=True,
            ),
        ),
        sa.Column("risk_score", sa.Float()),
        sa.Column("summary", sa.Text()),
        sa.Column(
            "recommendation",
            sa.Enum(
                "APPROVE",
                "COMMENT",
                "REQUEST_CHANGES",
                name=op.f("ck_reviews_recommendation"),
                native_enum=False,
                create_constraint=True,
            ),
        ),
        sa.Column("error_message", sa.String(2000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
        sa.UniqueConstraint("delivery_id", name=op.f("uq_reviews_delivery_id")),
        sa.ForeignKeyConstraint(
            ["pull_request_id"],
            ["pull_requests.id"],
            ondelete="CASCADE",
            name=op.f("fk_reviews_pull_request_id_pull_requests"),
        ),
        sa.CheckConstraint(
            "risk_score IS NULL OR risk_score >= 0", name=op.f("ck_reviews_nonnegative_risk_score")
        ),
        sa.CheckConstraint(
            "(status IN ('COMPLETED', 'PARTIAL', 'FAILED') AND completed_at IS NOT NULL) OR "
            "(status IN ('QUEUED', 'PROCESSING') AND completed_at IS NULL)",
            name=op.f("ck_reviews_completion_timestamp"),
        ),
        sa.CheckConstraint(
            "(status IN ('COMPLETED', 'PARTIAL') AND risk IS NOT NULL AND risk_score IS NOT NULL "
            "AND summary IS NOT NULL AND recommendation IS NOT NULL) OR "
            "(status NOT IN ('COMPLETED', 'PARTIAL') AND risk IS NULL AND risk_score IS NULL "
            "AND summary IS NULL AND recommendation IS NULL)",
            name=op.f("ck_reviews_result_matches_status"),
        ),
        sa.CheckConstraint(
            "(status = 'FAILED' AND error_message IS NOT NULL) OR "
            "(status <> 'FAILED' AND error_message IS NULL)",
            name=op.f("ck_reviews_error_matches_status"),
        ),
    )
    op.create_index("ix_reviews_pull_request_created", "reviews", ["pull_request_id", "created_at"])
    op.create_table(
        "findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column(
            "agent_type",
            sa.Enum(
                "security",
                "quality",
                "testing",
                "documentation",
                name=op.f("ck_findings_agent_type"),
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("line_number", sa.Integer()),
        sa.Column(
            "severity",
            sa.Enum(
                "LOW",
                "MEDIUM",
                "HIGH",
                "CRITICAL",
                name=op.f("ck_findings_severity"),
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_findings")),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["reviews.id"],
            ondelete="CASCADE",
            name=op.f("fk_findings_review_id_reviews"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_findings_confidence_range")
        ),
        sa.CheckConstraint(
            "line_number IS NULL OR line_number > 0", name=op.f("ck_findings_positive_line")
        ),
    )
    op.create_index("ix_findings_review_id", "findings", ["review_id"])


def downgrade() -> None:
    op.drop_index("ix_findings_review_id", table_name="findings")
    op.drop_table("findings")
    op.drop_index("ix_reviews_pull_request_created", table_name="reviews")
    op.drop_table("reviews")
    op.drop_table("pull_requests")
    op.drop_table("repositories")
