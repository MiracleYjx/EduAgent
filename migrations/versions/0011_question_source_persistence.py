"""Persist question source snapshots, generation metadata and revision comments.

Revision ID: 0011_question_source_persistence
Revises: 0010_review_round_ids

Existing questions are intentionally not backfilled: their original sources are unknown.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_question_source_persistence"
down_revision: str | None = "0010_review_round_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """仅创建本 revision 的三张表及其约束、索引。"""
    op.create_table(
        "question_source_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("live_chunk_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("content_snapshot", sa.Text(), nullable=False),
        sa.Column("source_file", sa.String(length=255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("retrieval_rank", sa.Integer(), nullable=True),
        sa.Column("score_kind", sa.String(length=32), nullable=True),
        sa.Column("score_value", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["live_chunk_id"], ["document_chunks.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "question_id", "chunk_id", name="uq_question_source_chunks_question_chunk"
        ),
        sa.UniqueConstraint(
            "question_id",
            "source_order",
            name="uq_question_source_chunks_question_order",
        ),
    )
    op.create_index(
        "ix_question_source_chunks_course_question",
        "question_source_chunks",
        ["course_id", "question_id"],
    )

    op.create_table(
        "question_generation_metadata",
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=True),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("retrieval_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("question_id"),
    )

    op.create_table(
        "question_revision_comments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("commented_by", sa.Uuid(), nullable=False),
        sa.Column(
            "commented_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(comment)) > 0 AND length(comment) <= 2000",
            name="ck_question_revision_comments_content_length",
        ),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["commented_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_question_revision_comments_question_commented_at",
        "question_revision_comments",
        ["question_id", "commented_at"],
    )


def downgrade() -> None:
    """仅撤销本 revision 新增的对象，不删除原有题目或片段。"""
    op.drop_index(
        "ix_question_revision_comments_question_commented_at",
        table_name="question_revision_comments",
    )
    op.drop_table("question_revision_comments")
    op.drop_table("question_generation_metadata")
    op.drop_index(
        "ix_question_source_chunks_course_question",
        table_name="question_source_chunks",
    )
    op.drop_table("question_source_chunks")
