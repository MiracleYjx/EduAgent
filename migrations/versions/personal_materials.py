"""Personal materials: subject-owned knowledge bases, chunks and parse facts.

T028/T030 need a real subject-scoped index: legacy ``knowledge_bases`` and
``document_chunks`` were course-owned, which made personal teaching retrieval
impossible without a course. This revision adds the subject owner, keeps the
legacy course path intact (``NULL`` subject keeps the old constraints
unchecked via MATCH SIMPLE), and records material state, optional metadata and
parse facts on ``documents``.

No legacy row is rewritten or deleted. Downgrade fails closed because dropping
these columns would silently lose personal parse/index facts.
"""

from alembic import op

revision = "0028_personal_materials"
down_revision = "0027_personal_content_assets"
branch_labels = None
depends_on = None


def upgrade():
    # --- knowledge bases: one personal knowledge base per subject -----------
    op.execute("ALTER TABLE knowledge_bases ALTER COLUMN course_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE knowledge_bases ADD COLUMN subject_id UUID "
        "REFERENCES subjects(id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE knowledge_bases ADD CONSTRAINT uq_knowledge_bases_id_subject "
        "UNIQUE(id, subject_id)"
    )
    op.execute(
        "ALTER TABLE knowledge_bases ADD CONSTRAINT uq_knowledge_bases_subject "
        "UNIQUE(subject_id)"
    )
    op.execute(
        "ALTER TABLE knowledge_bases ADD CONSTRAINT ck_knowledge_base_domain CHECK ("
        "(course_id IS NOT NULL AND subject_id IS NULL) OR "
        "(course_id IS NULL AND subject_id IS NOT NULL))"
    )

    # --- documents: purpose split + personal facts -------------------------
    op.execute(
        "ALTER TABLE documents ADD COLUMN material_state VARCHAR(32)"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN optional_metadata JSONB"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN parse_facts JSONB"
    )
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT ck_document_optional_metadata_object "
        "CHECK (optional_metadata IS NULL "
        "OR jsonb_typeof(optional_metadata) = 'object')"
    )
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT ck_document_parse_facts_object "
        "CHECK (parse_facts IS NULL OR jsonb_typeof(parse_facts) = 'object')"
    )
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT ck_document_material_state CHECK ("
        "material_state IS NULL OR material_state IN "
        "('uploaded','parsed','indexed','failed'))"
    )
    # Personal documents must point at the personal knowledge base of their own
    # subject; legacy rows keep subject_id NULL and are not re-checked.
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT fk_document_personal_knowledge_base "
        "FOREIGN KEY(knowledge_base_id, subject_id) "
        "REFERENCES knowledge_bases (id, subject_id) ON DELETE RESTRICT"
    )

    # --- chunks: subject owner and subject-scoped integrity ----------------
    op.execute("ALTER TABLE document_chunks ALTER COLUMN course_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN subject_id UUID "
        "REFERENCES subjects(id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE document_chunks ADD CONSTRAINT ck_document_chunk_domain CHECK ("
        "(course_id IS NOT NULL AND subject_id IS NULL) OR "
        "(course_id IS NULL AND subject_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE document_chunks ADD CONSTRAINT fk_chunk_personal_knowledge_base "
        "FOREIGN KEY(knowledge_base_id, subject_id) "
        "REFERENCES knowledge_bases (id, subject_id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE document_chunks ADD CONSTRAINT fk_chunk_personal_document "
        "FOREIGN KEY(document_id, subject_id) "
        "REFERENCES documents (id, subject_id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE document_chunks ADD CONSTRAINT fk_chunk_personal_chapter "
        "FOREIGN KEY(chapter_id, subject_id) "
        "REFERENCES chapters (id, subject_id) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_subject_chapter "
        "ON document_chunks (subject_id, chapter_id)"
    )

    # A material version must belong to the same subject as its knowledge base
    # owner; versions themselves stay immutable (0025 trigger).
    op.execute(
        "CREATE INDEX ix_documents_subject_purpose "
        "ON documents (subject_id, purpose)"
    )


def downgrade():
    raise RuntimeError(
        "Personal material history requires an explicit preservation plan"
    )
