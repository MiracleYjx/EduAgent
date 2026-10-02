"""T153 paper import/correction foundation; preserve unknown legacy sources."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_paper_import"
down_revision: str | None = "0013_file_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name, *values, length=32):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _times():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.add_column("documents", sa.Column("purpose", _enum("document_purpose", "knowledge_base", "paper_source"), nullable=False, server_default="knowledge_base"))
    op.alter_column("documents", "knowledge_base_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint("ck_document_purpose_knowledge_base", "documents", "(purpose = 'knowledge_base' AND knowledge_base_id IS NOT NULL) OR (purpose = 'paper_source' AND knowledge_base_id IS NULL)")
    op.create_index("ix_documents_course_purpose", "documents", ["course_id", "purpose"])
    op.add_column("questions", sa.Column("source_type", _enum("question_source_type", "manual", "ai_generated", "paper_imported", "adapted")))
    op.add_column("questions", sa.Column("analysis", sa.Text()))
    op.add_column("questions", sa.Column("frozen_at", sa.DateTime(timezone=True)))
    op.create_table(
        "paper_imports",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("course_id", sa.Uuid(), sa.ForeignKey("courses.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column("status", _enum("paper_import_status", "Uploaded", "Parsing", "Extracting", "Pending Review", "Ready", "Failed", "Rejected"), nullable=False, server_default="Uploaded"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        *_times(),
        sa.CheckConstraint("page_count IS NULL OR page_count BETWEEN 1 AND 50", name="ck_paper_import_page_count"),
        sa.CheckConstraint("status NOT IN ('Pending Review', 'Ready') OR page_count IS NOT NULL", name="ck_paper_import_known_pages"),
        sa.CheckConstraint("status <> 'Failed' OR (error_code IS NOT NULL AND error_message IS NOT NULL)", name="ck_paper_import_failure"),
    )
    op.create_index("ix_paper_imports_course_created", "paper_imports", ["course_id", "created_at"])
    op.create_index("ix_paper_imports_course_status", "paper_imports", ["course_id", "status"])
    op.create_table(
        "source_pages",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("paper_import_id", sa.Uuid(), sa.ForeignKey("paper_imports.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(1024), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("ocr_text", sa.Text()),
        sa.Column("ocr_confidence", sa.Numeric(5, 4)),
        sa.Column("file_metadata", postgresql.JSONB(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("paper_import_id", "page_number", name="uq_source_pages_import_number"),
        sa.CheckConstraint("page_number >= 1 AND width > 0 AND height > 0", name="ck_source_page_dimensions"),
        sa.CheckConstraint("ocr_confidence IS NULL OR ocr_confidence BETWEEN 0 AND 1", name="ck_source_page_confidence"),
        sa.CheckConstraint("file_metadata IS NULL OR jsonb_typeof(file_metadata) = 'object'", name="ck_source_page_metadata"),
    )
    op.create_table(
        "extracted_questions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("paper_import_id", sa.Uuid(), sa.ForeignKey("paper_imports.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_page_ids", postgresql.JSONB(none_as_null=True), nullable=False, server_default="[]"),
        sa.Column("question_type", _enum("extracted_question_type", "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE", "FILL_BLANK", "SHORT_ANSWER", "ESSAY")),
        sa.Column("content", sa.Text()),
        sa.Column("options", postgresql.JSONB(none_as_null=True)),
        sa.Column("reference_answer", sa.Text()),
        sa.Column("scoring_rubric", sa.Text()),
        sa.Column("score", sa.Numeric(8, 2)),
        sa.Column("status", _enum("extracted_question_status", "Extracted", "Pending Correction", "Corrected", "Rejected"), nullable=False, server_default="Extracted"),
        sa.Column("correction_notes", sa.Text()),
        sa.Column("extracted_by", _enum("extracted_by", "OCR", "LLM", "TEXT", length=16), nullable=False),
        sa.Column("extraction_confidence", sa.Numeric(5, 4)),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id", ondelete="RESTRICT"), unique=True),
        sa.Column("question_number", sa.Text()),
        sa.Column("analysis", sa.Text()),
        sa.Column("knowledge_points", postgresql.JSONB(none_as_null=True)),
        sa.Column("source_regions", postgresql.JSONB(none_as_null=True)),
        sa.Column("assets", postgresql.JSONB(none_as_null=True)),
        sa.Column("image_assessment", postgresql.JSONB(none_as_null=True)),
        *_times(),
        sa.CheckConstraint("score IS NULL OR score > 0", name="ck_extracted_question_score"),
        sa.CheckConstraint("extraction_confidence IS NULL OR extraction_confidence BETWEEN 0 AND 1", name="ck_extracted_question_confidence"),
        sa.CheckConstraint("(status = 'Corrected' AND question_id IS NOT NULL) OR (status <> 'Corrected' AND question_id IS NULL)", name="ck_extracted_question_link"),
        sa.CheckConstraint("status <> 'Rejected' OR length(trim(correction_notes)) > 0 AND correction_notes IS NOT NULL", name="ck_extracted_question_rejection"),
        *[sa.CheckConstraint(f"{field} IS NULL OR jsonb_typeof({field}) = 'array'", name=f"ck_extracted_{field}_array") for field in ("source_page_ids", "knowledge_points", "source_regions", "assets")],
        sa.CheckConstraint("assets IS NULL OR jsonb_array_length(assets) <= 5", name="ck_extracted_assets_count"),
        sa.CheckConstraint("image_assessment IS NULL OR jsonb_typeof(image_assessment) = 'object'", name="ck_extracted_image_assessment"),
    )
    op.create_index("ix_extracted_questions_import_status", "extracted_questions", ["paper_import_id", "status"])
    op.create_index("ix_extracted_questions_assets", "extracted_questions", ["assets"], postgresql_using="gin")


def downgrade() -> None:
    # Never silently discard imported material or formal corrections.
    connection = op.get_bind()
    for table in ("paper_imports", "source_pages", "extracted_questions"):
        if connection.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")):
            raise RuntimeError("存在试卷导入数据，须人工迁移后才能降级。")
    if connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM documents WHERE purpose = 'paper_source')")):
        raise RuntimeError("存在原试卷文件，不能恢复知识库非空约束。")
    op.drop_table("extracted_questions")
    op.drop_table("source_pages")
    op.drop_table("paper_imports")
    for field in ("frozen_at", "analysis"):
        op.drop_column("questions", field)
    op.drop_constraint("question_source_type", "questions", type_="check")
    op.drop_column("questions", "source_type")
    op.drop_index("ix_documents_course_purpose", table_name="documents")
    op.drop_constraint("ck_document_purpose_knowledge_base", "documents", type_="check")
    op.drop_constraint("document_purpose", "documents", type_="check")
    op.drop_column("documents", "purpose")
    op.alter_column("documents", "knowledge_base_id", existing_type=sa.Uuid(), nullable=False)
