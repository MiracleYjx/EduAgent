"""Prepare durable cutover facts; normal upgrade NEVER disposes legacy objects.

Physical retirement is an explicitly authorized offline operation. Its journal,
not the Alembic revision alone, states whether disposal and activation completed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0033_personal_cutover"
down_revision = "0032_personal_conversations"
branch_labels = None
depends_on = None

LEGACY_TABLES = frozenset({
    "users", "roles", "user_roles", "courses", "exams", "exam_participants",
    "exam_questions", "submissions", "answers", "grading_results", "review_records",
    "exam_results", "diagnosis_reports", "export_files", "question_revision_comments",
    "question_source_papers",
})


def upgrade() -> None:
    op.create_table(
        "personal_cutovers",
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("plan", JSONB(), nullable=False),
        sa.Column("file_progress", JSONB(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("state IN ('prepared','running','failed','complete')", name="ck_personal_cutover_state"),
    )


def detach_approved_foreign_keys(connection, schema: str, foreign_keys: list[dict]) -> None:
    """Only the preflight's exact keys touching the approved sixteen tables."""
    quote = connection.dialect.identifier_preparer.quote
    for key in foreign_keys:
        if key["table"] not in LEGACY_TABLES and key["target"] not in LEGACY_TABLES:
            raise ValueError("CUTOVER_UNAPPROVED_CONSTRAINT")
        connection.execute(sa.text(
            f'ALTER TABLE {quote(schema)}.{quote(key["table"])} DROP CONSTRAINT {quote(key["name"])}'
        ))


def retire_approved_tables(connection, schema: str) -> None:
    """No CASCADE, no enums/functions/history removal, no workflow_runs drop."""
    quote = connection.dialect.identifier_preparer.quote
    for name in sorted(LEGACY_TABLES):
        connection.execute(sa.text(f"DROP TABLE {quote(schema)}.{quote(name)}"))


def downgrade() -> None:
    raise RuntimeError("Cutover disposal is irreversible; downgrade cannot recover abandoned data")
