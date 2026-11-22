"""eval results and message feedback

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_results",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("faithfulness", sa.Float(), nullable=True),
        sa.Column("answer_relevance", sa.Float(), nullable=True),
        sa.Column("context_precision", sa.Float(), nullable=True),
        sa.Column("citation_accuracy", sa.Float(), nullable=True),
        sa.Column("answer_alias", sa.String(length=64), nullable=True),
        sa.Column("answer_deployment", sa.String(length=128), nullable=True),
        sa.Column("answer_provider", sa.String(length=64), nullable=True),
        sa.Column("judge_deployment", sa.String(length=256), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_eval_results_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_eval_results_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_results")),
        sa.UniqueConstraint("message_id", name=op.f("uq_eval_results_message_id")),
    )
    op.create_index(
        op.f("ix_eval_results_answer_deployment"),
        "eval_results",
        ["answer_deployment"],
        unique=False,
    )
    op.create_index(op.f("ix_eval_results_run_id"), "eval_results", ["run_id"], unique=False)
    op.create_index(op.f("ix_eval_results_tenant_id"), "eval_results", ["tenant_id"], unique=False)
    op.create_index(
        "ix_eval_results_tenant_id_created_at",
        "eval_results",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "message_feedback",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("rating IN (-1, 1)", name=op.f("ck_message_feedback_rating_sign")),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_feedback_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_message_feedback_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_message_feedback_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_feedback")),
        sa.UniqueConstraint("message_id", "user_id", name="uq_message_feedback_message_user"),
    )
    op.create_index(
        op.f("ix_message_feedback_message_id"), "message_feedback", ["message_id"], unique=False
    )
    op.create_index(
        op.f("ix_message_feedback_tenant_id"), "message_feedback", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_message_feedback_tenant_id"), table_name="message_feedback")
    op.drop_index(op.f("ix_message_feedback_message_id"), table_name="message_feedback")
    op.drop_table("message_feedback")
    op.drop_index("ix_eval_results_tenant_id_created_at", table_name="eval_results")
    op.drop_index(op.f("ix_eval_results_tenant_id"), table_name="eval_results")
    op.drop_index(op.f("ix_eval_results_run_id"), table_name="eval_results")
    op.drop_index(op.f("ix_eval_results_answer_deployment"), table_name="eval_results")
    op.drop_table("eval_results")
