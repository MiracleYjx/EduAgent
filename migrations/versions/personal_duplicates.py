"""Persist only real duplicate decisions; retain both histories."""
from alembic import op

revision = "0030_personal_duplicates"
down_revision = "0029_personal_paper_import"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE duplicate_candidates (
        id UUID PRIMARY KEY,
        subject_id UUID NOT NULL,
        left_id UUID NOT NULL,
        right_id UUID NOT NULL,
        left_revision INTEGER NOT NULL,
        right_revision INTEGER NOT NULL,
        decision VARCHAR(16) NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(subject_id,left_id,right_id,left_revision,right_revision),
        FOREIGN KEY(left_id,subject_id) REFERENCES questions(id,subject_id) ON DELETE RESTRICT,
        FOREIGN KEY(right_id,subject_id) REFERENCES questions(id,subject_id) ON DELETE RESTRICT,
        CONSTRAINT ck_duplicate_pair_order CHECK (left_id < right_id),
        CONSTRAINT ck_duplicate_revisions CHECK (left_revision>0 AND right_revision>0),
        CONSTRAINT ck_duplicate_decision CHECK (decision IN ('pending','same','different'))
    )""")


def downgrade():
    raise RuntimeError("DUPLICATE_HISTORY_REQUIRES_EXPLICIT_MIGRATION")
