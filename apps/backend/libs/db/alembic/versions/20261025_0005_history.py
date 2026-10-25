"""conversation summaries, memory and context capsules

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_capsules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("source_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("source_model", sa.String(128), nullable=True),
        sa.Column("share_token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_context_capsules_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_context_capsules_created_by_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["conversations.id"],
            name=op.f("fk_context_capsules_source_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_capsules")),
        sa.UniqueConstraint("share_token", name=op.f("uq_context_capsules_share_token")),
    )
    op.create_index(op.f("ix_context_capsules_tenant_id"), "context_capsules", ["tenant_id"])
    op.create_index(
        "ix_context_capsules_created_by_created_at",
        "context_capsules",
        ["created_by", "created_at"],
    )

    op.add_column("conversations", sa.Column("alias", sa.String(64), nullable=True))
    op.add_column("conversations", sa.Column("capsule_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_conversations_capsule_id_context_capsules"),
        "conversations",
        "context_capsules",
        ["capsule_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("covers_until_msg_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_conversation_summaries_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_summaries_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_summaries")),
        sa.UniqueConstraint("conversation_id", "version", name="uq_conversation_summaries_version"),
    )
    op.create_index(
        op.f("ix_conversation_summaries_tenant_id"), "conversation_summaries", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_conversation_summaries_conversation_id"),
        "conversation_summaries",
        ["conversation_id"],
    )

    op.create_table(
        "conversation_memory",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_conversation_memory_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_memory_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_conversation_memory")),
    )
    op.create_index(
        op.f("ix_conversation_memory_tenant_id"), "conversation_memory", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_table("conversation_memory")
    op.drop_table("conversation_summaries")
    op.drop_constraint(
        op.f("fk_conversations_capsule_id_context_capsules"), "conversations", type_="foreignkey"
    )
    op.drop_column("conversations", "capsule_id")
    op.drop_column("conversations", "alias")
    op.drop_table("context_capsules")
