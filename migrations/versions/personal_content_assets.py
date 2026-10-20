"""Freeze media/source facts used by personal history; retain the legacy chain."""

from alembic import op

revision = "0027_personal_content_assets"
down_revision = "0026_learning_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE question_assets ADD COLUMN source_document_id UUID REFERENCES documents(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE question_assets ADD COLUMN source_anchor TEXT")
    op.execute("ALTER TABLE question_assets ADD CONSTRAINT ck_question_asset_anchor CHECK ((source_document_id IS NULL) = (source_anchor IS NULL))")
    op.execute("""
    CREATE FUNCTION personal_content_asset_binding() RETURNS trigger AS $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM question_revisions r JOIN question_assets a
          ON a.question_id=r.question_id
        WHERE r.id=NEW.revision_id AND a.id=NEW.asset_id
      ) THEN RAISE EXCEPTION 'personal asset must belong to the revision question';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER personal_content_asset_binding BEFORE INSERT OR UPDATE
    ON question_revision_assets FOR EACH ROW EXECUTE FUNCTION personal_content_asset_binding()
    """)
    op.execute("""
    CREATE FUNCTION personal_content_history() RETURNS trigger AS $$
    BEGIN
      IF TG_TABLE_NAME='question_assets' THEN
        IF TG_OP='UPDATE' AND NEW.question_id<>OLD.question_id AND EXISTS (
          SELECT 1 FROM question_revision_assets a JOIN question_revisions r ON r.id=a.revision_id
          WHERE a.asset_id=OLD.id AND r.question_id<>NEW.question_id
        ) THEN RAISE EXCEPTION 'bound personal asset cannot change question'; END IF;
        IF EXISTS (SELECT 1 FROM question_revision_assets a JOIN practice_items p
          ON p.question_revision_id=a.revision_id WHERE a.asset_id=OLD.id)
        THEN RAISE EXCEPTION 'personal historical media is immutable'; END IF;
      ELSE
        IF EXISTS (SELECT 1 FROM practice_items WHERE question_id=OLD.question_id)
        THEN RAISE EXCEPTION 'personal historical source is immutable'; END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    for table in ("question_assets", "question_origins"):
        op.execute(f"CREATE TRIGGER personal_content_history BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION personal_content_history()")


def downgrade():
    raise RuntimeError("Personal content history requires an explicit preservation plan")
