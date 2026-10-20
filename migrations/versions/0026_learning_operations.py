"""Persist personal operation plans and pre-send reservations."""

from alembic import op

revision = "0026_learning_operations"
down_revision = "0025_personal_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE operation_runs (
	environment_id UUID NOT NULL, 
	idempotency_key VARCHAR(160) NOT NULL, 
	purpose VARCHAR(80) NOT NULL, 
	preview_digest VARCHAR(64) NOT NULL, 
	plan_digest VARCHAR(64) NOT NULL, 
	runtime_snapshot_id UUID NOT NULL, 
	state VARCHAR(32) DEFAULT 'prepared' NOT NULL, 
	request_budget INTEGER NOT NULL, 
	actual_requests INTEGER DEFAULT '0' NOT NULL, 
	steps JSONB NOT NULL, 
	active_slot VARCHAR(16), 
	retry_of_id UUID, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (environment_id, idempotency_key), 
	UNIQUE (environment_id, active_slot), 
	CONSTRAINT ck_operation_requests CHECK (request_budget > 0 AND actual_requests >= 0 AND actual_requests <= request_budget), 
	CONSTRAINT ck_operation_state CHECK (state IN ('prepared','confirmed','running','succeeded','failed','partial','outcome_unknown'))
)""")


def downgrade():
    raise RuntimeError("OPERATION_HISTORY_DOWNGRADE_REQUIRES_EXPLICIT_REVIEW")
