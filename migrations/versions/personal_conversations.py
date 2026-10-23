"""Add subject conversations without changing learning or grading history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0032_personal_conversations"
down_revision = "0031_personal_grading_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    common = lambda: [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]
    op.create_table(
        "conversations",
        *common(),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("fee_conditions", sa.String(64)),
        sa.Column("pending_operation_id", sa.Uuid()),
    )
    op.create_index("ix_conversations_subject_id", "conversations", ["subject_id"])
    op.create_table(
        "conversation_messages",
        *common(),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(160)),
        sa.Column("request", JSONB()),
        sa.Column("context_digest", sa.String(64)),
        sa.Column("fee_digest", sa.String(64)),
        sa.Column("result", JSONB()),
        sa.Column("citations", JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("runtime_snapshot_id", sa.Uuid()),
        sa.UniqueConstraint("conversation_id", "request_key"),
        sa.UniqueConstraint("operation_id", "role"),
        sa.CheckConstraint(
            "role IN ('user','assistant')", name="ck_conversation_message_role"
        ),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id",
        "conversation_messages",
        ["conversation_id"],
    )


def downgrade() -> None:
    raise RuntimeError("Conversation history requires an explicit preservation plan")
