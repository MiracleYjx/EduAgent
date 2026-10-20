"""Subject-owned personal paper staging; preserve legacy course imports."""
from alembic import op

revision = "0029_personal_paper_import"
down_revision = "0028_personal_materials"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE paper_imports ADD COLUMN subject_id UUID REFERENCES subjects(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE paper_imports ALTER COLUMN course_id DROP NOT NULL")
    op.execute("ALTER TABLE paper_imports ALTER COLUMN uploaded_by DROP NOT NULL")
    op.execute("""ALTER TABLE paper_imports ADD CONSTRAINT ck_paper_import_domain CHECK (
        (subject_id IS NULL AND course_id IS NOT NULL AND uploaded_by IS NOT NULL) OR
        (subject_id IS NOT NULL AND course_id IS NULL AND uploaded_by IS NULL))""")
    op.execute("""ALTER TABLE paper_imports ADD CONSTRAINT fk_paper_import_subject_document
        FOREIGN KEY(document_id,subject_id) REFERENCES documents(id,subject_id) ON DELETE RESTRICT""")
    op.execute("ALTER TABLE paper_imports DROP CONSTRAINT ck_paper_import_page_count")
    op.execute("""ALTER TABLE paper_imports ADD CONSTRAINT ck_paper_import_page_count CHECK (
        page_count IS NULL OR (page_count >= 1 AND (subject_id IS NOT NULL OR page_count <= 50)))""")
    op.execute("ALTER TABLE paper_imports DROP CONSTRAINT ck_paper_import_known_pages")
    op.execute("""ALTER TABLE paper_imports ADD CONSTRAINT ck_paper_import_known_pages CHECK (
        subject_id IS NOT NULL OR status NOT IN ('Pending Review','Ready') OR page_count IS NOT NULL)""")
    op.execute("CREATE INDEX ix_paper_imports_subject ON paper_imports(subject_id)")
    op.execute("ALTER TABLE paper_imports ADD COLUMN extraction_operation_id UUID REFERENCES operation_runs(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE extracted_questions ADD COLUMN personal_content JSONB")
    op.execute("ALTER TABLE extracted_questions ADD COLUMN edit_revision INTEGER NOT NULL DEFAULT 1")
    op.execute("""ALTER TABLE extracted_questions ADD CONSTRAINT ck_extracted_personal_content CHECK (
        personal_content IS NULL OR jsonb_typeof(personal_content)='object')""")
    op.execute("ALTER TABLE extracted_questions ADD CONSTRAINT ck_extracted_edit_revision CHECK (edit_revision >= 1)")


def downgrade():
    raise RuntimeError("PERSONAL_PAPER_HISTORY_REQUIRES_EXPLICIT_MIGRATION")
