"""创建 M1 业务模型

Revision ID: 7c00941eb6e3
Revises: 0001_enable_pgvector
Create Date: 2026-09-07 12:03:52.807992

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c00941eb6e3"
down_revision: str | None = "0001_enable_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Alembic 根据 T015 模型生成的业务表结构。
    op.create_table(
        "roles",
        sa.Column(
            "name",
            sa.Enum(
                "Teacher",
                "Student",
                "Admin",
                name="user_role",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.String(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "users",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)
    op.create_table(
        "courses",
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_courses_created_by"), "courses", ["created_by"], unique=False
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
    )
    op.create_table(
        "exams",
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "Draft",
                "Published",
                "Closed",
                "Archived",
                name="exam_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
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
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_exams_course_id"), "exams", ["course_id"], unique=False)
    op.create_index(op.f("ix_exams_created_by"), "exams", ["created_by"], unique=False)
    op.create_index(op.f("ix_exams_status"), "exams", ["status"], unique=False)
    op.create_table(
        "knowledge_bases",
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("course_id", "name", name="uq_knowledge_bases_course_name"),
    )
    op.create_index(
        op.f("ix_knowledge_bases_course_id"),
        "knowledge_bases",
        ["course_id"],
        unique=False,
    )
    op.create_table(
        "questions",
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "SINGLE_CHOICE",
                "MULTIPLE_CHOICE",
                "TRUE_FALSE",
                "FILL_BLANK",
                "SHORT_ANSWER",
                "ESSAY",
                name="question_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("reference_answer", sa.Text(), nullable=True),
        sa.Column("scoring_rubric", sa.Text(), nullable=True),
        sa.Column("difficulty", sa.Text(), nullable=True),
        sa.Column("knowledge_points", sa.JSON(), nullable=False),
        sa.Column("score", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "Draft",
                "Candidate Generation",
                "Pending Review",
                "Approved",
                "Needs Revision",
                "Published",
                name="question_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_questions_course_id"), "questions", ["course_id"], unique=False
    )
    op.create_index(
        "ix_questions_course_status", "questions", ["course_id", "status"], unique=False
    )
    op.create_index(
        op.f("ix_questions_created_by"), "questions", ["created_by"], unique=False
    )
    op.create_index(op.f("ix_questions_status"), "questions", ["status"], unique=False)
    op.create_index(op.f("ix_questions_type"), "questions", ["type"], unique=False)
    op.create_table(
        "documents",
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("file_format", sa.String(length=32), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "Uploaded",
                "Parsing",
                "Chunking",
                "Embedding",
                "Ready",
                "Failed",
                name="document_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
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
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_documents_course_id"), "documents", ["course_id"], unique=False
    )
    op.create_index(
        op.f("ix_documents_knowledge_base_id"),
        "documents",
        ["knowledge_base_id"],
        unique=False,
    )
    op.create_index(op.f("ix_documents_status"), "documents", ["status"], unique=False)
    op.create_table(
        "exam_questions",
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("exam_id", "question_id"),
    )
    op.create_table(
        "submissions",
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "Draft",
                "Submitted",
                "Graded",
                "Reviewed",
                name="submission_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("graded_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
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
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_submissions_exam_id"), "submissions", ["exam_id"], unique=False
    )
    op.create_index(
        op.f("ix_submissions_status"), "submissions", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_submissions_student_id"), "submissions", ["student_id"], unique=False
    )
    op.create_table(
        "answers",
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "Draft",
                "Submitted",
                "Grading",
                "Graded",
                "Failed",
                name="answer_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id", "question_id", name="uq_answers_submission_question"
        ),
    )
    op.create_index(
        op.f("ix_answers_question_id"), "answers", ["question_id"], unique=False
    )
    op.create_index(op.f("ix_answers_status"), "answers", ["status"], unique=False)
    op.create_index(
        op.f("ix_answers_submission_id"), "answers", ["submission_id"], unique=False
    )
    # 业务表结构迁移结束。


def downgrade() -> None:
    # Alembic 根据 T015 模型生成的业务表结构。
    op.drop_index(op.f("ix_answers_submission_id"), table_name="answers")
    op.drop_index(op.f("ix_answers_status"), table_name="answers")
    op.drop_index(op.f("ix_answers_question_id"), table_name="answers")
    op.drop_table("answers")
    op.drop_index(op.f("ix_submissions_student_id"), table_name="submissions")
    op.drop_index(op.f("ix_submissions_status"), table_name="submissions")
    op.drop_index(op.f("ix_submissions_exam_id"), table_name="submissions")
    op.drop_table("submissions")
    op.drop_table("exam_questions")
    op.drop_index(op.f("ix_documents_status"), table_name="documents")
    op.drop_index(op.f("ix_documents_knowledge_base_id"), table_name="documents")
    op.drop_index(op.f("ix_documents_course_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_questions_type"), table_name="questions")
    op.drop_index(op.f("ix_questions_status"), table_name="questions")
    op.drop_index(op.f("ix_questions_created_by"), table_name="questions")
    op.drop_index("ix_questions_course_status", table_name="questions")
    op.drop_index(op.f("ix_questions_course_id"), table_name="questions")
    op.drop_table("questions")
    op.drop_index(op.f("ix_knowledge_bases_course_id"), table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
    op.drop_index(op.f("ix_exams_status"), table_name="exams")
    op.drop_index(op.f("ix_exams_created_by"), table_name="exams")
    op.drop_index(op.f("ix_exams_course_id"), table_name="exams")
    op.drop_table("exams")
    op.drop_table("user_roles")
    op.drop_index(op.f("ix_courses_created_by"), table_name="courses")
    op.drop_table("courses")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
    op.drop_table("roles")
    # 业务表结构迁移结束。
