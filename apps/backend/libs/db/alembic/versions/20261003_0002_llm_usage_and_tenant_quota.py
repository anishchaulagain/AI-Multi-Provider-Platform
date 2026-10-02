"""llm usage log and per-tenant daily token quota

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("daily_token_quota", sa.BigInteger(), nullable=True))

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("deployment", sa.String(128), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cache", sa.String(16), nullable=False),
        sa.Column("fallbacks", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_llm_usage_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_usage")),
    )
    op.create_index(op.f("ix_llm_usage_tenant_id"), "llm_usage", ["tenant_id"])
    op.create_index(op.f("ix_llm_usage_user_id"), "llm_usage", ["user_id"])
    op.create_index("ix_llm_usage_tenant_id_created_at", "llm_usage", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_table("llm_usage")
    op.drop_column("tenants", "daily_token_quota")
