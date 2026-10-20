"""Add personal foundation without copying or removing legacy rows."""

from alembic import op

revision = "0025_personal_foundation"
down_revision = "0024_embedding_index_identity"
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE subjects (
	name VARCHAR(160) NOT NULL, 
	description VARCHAR, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
)""",
    """ALTER TABLE questions ALTER COLUMN course_id DROP NOT NULL""",
    """ALTER TABLE questions ALTER COLUMN created_by DROP NOT NULL""",
    """ALTER TABLE questions ALTER COLUMN type DROP NOT NULL""",
    """ALTER TABLE questions ALTER COLUMN content DROP NOT NULL""",
    """ALTER TABLE questions ALTER COLUMN score DROP NOT NULL""",
    """ALTER TABLE questions ADD COLUMN subject_id UUID REFERENCES subjects(id) ON DELETE RESTRICT""",
    """ALTER TABLE questions ADD CONSTRAINT uq_questions_id_subject UNIQUE(id,subject_id)""",
    """ALTER TABLE questions ADD COLUMN current_revision_id UUID""",
    """ALTER TABLE questions ADD COLUMN canonical_id UUID""",
    """ALTER TABLE questions ADD CONSTRAINT ck_question_domain CHECK ((subject_id IS NULL AND course_id IS NOT NULL AND created_by IS NOT NULL AND content IS NOT NULL AND type IS NOT NULL AND score IS NOT NULL) OR (subject_id IS NOT NULL AND course_id IS NULL AND created_by IS NULL))""",
    """ALTER TABLE questions ADD CONSTRAINT fk_question_canonical FOREIGN KEY(canonical_id, subject_id) REFERENCES questions (id, subject_id)""",
    """ALTER TABLE documents ALTER COLUMN course_id DROP NOT NULL""",
    """ALTER TABLE documents ALTER COLUMN uploaded_by DROP NOT NULL""",
    """ALTER TABLE documents ADD COLUMN subject_id UUID REFERENCES subjects(id) ON DELETE RESTRICT""",
    """ALTER TABLE documents ADD CONSTRAINT uq_documents_id_subject UNIQUE(id,subject_id)""",
    """ALTER TABLE documents ADD CONSTRAINT ck_document_domain CHECK ((subject_id IS NULL AND course_id IS NOT NULL AND uploaded_by IS NOT NULL) OR (subject_id IS NOT NULL AND course_id IS NULL AND uploaded_by IS NULL))""",
    """ALTER TABLE chapters ALTER COLUMN course_id DROP NOT NULL""",
    """ALTER TABLE chapters ALTER COLUMN confirmed_by DROP NOT NULL""",
    """ALTER TABLE chapters ALTER COLUMN confirmed_at DROP NOT NULL""",
    """ALTER TABLE chapters ADD COLUMN subject_id UUID REFERENCES subjects(id) ON DELETE RESTRICT""",
    """ALTER TABLE chapters ADD CONSTRAINT uq_chapters_id_subject UNIQUE(id,subject_id)""",
    """ALTER TABLE chapters ADD COLUMN parent_id UUID""",
    """ALTER TABLE chapters ADD CONSTRAINT fk_chapter_parent FOREIGN KEY(parent_id, subject_id) REFERENCES chapters (id, subject_id)""",
    """ALTER TABLE chapters ADD CONSTRAINT ck_chapter_domain CHECK ((subject_id IS NULL AND course_id IS NOT NULL AND confirmed_by IS NOT NULL) OR (subject_id IS NOT NULL AND course_id IS NULL AND confirmed_by IS NULL))""",
    """CREATE TABLE knowledge_points (
	subject_id UUID NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	confirmation_state VARCHAR(32) DEFAULT 'pending' NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id, subject_id), 
	FOREIGN KEY(subject_id) REFERENCES subjects (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE material_series (
	subject_id UUID NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	default_version_id UUID, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id, subject_id), 
	FOREIGN KEY(subject_id) REFERENCES subjects (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE practice_sessions (
	subject_id UUID NOT NULL, 
	state VARCHAR(32) DEFAULT 'preview' NOT NULL, 
	conditions JSONB NOT NULL, 
	submitted_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id, subject_id), 
	CONSTRAINT ck_practice_state CHECK (state IN ('preview','in_progress','submitted','grading','completed','partial_failed')), 
	FOREIGN KEY(subject_id) REFERENCES subjects (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE chapter_knowledge_points (
	chapter_id UUID NOT NULL, 
	knowledge_point_id UUID NOT NULL, 
	subject_id UUID NOT NULL, 
	PRIMARY KEY (chapter_id, knowledge_point_id), 
	FOREIGN KEY(chapter_id, subject_id) REFERENCES chapters (id, subject_id) ON DELETE RESTRICT, 
	FOREIGN KEY(knowledge_point_id, subject_id) REFERENCES knowledge_points (id, subject_id) ON DELETE RESTRICT
)""",
    """CREATE TABLE question_revisions (
	question_id UUID NOT NULL, 
	subject_id UUID NOT NULL, 
	revision INTEGER NOT NULL, 
	question_type VARCHAR(32) NOT NULL, 
	stem_blocks JSONB NOT NULL, 
	options JSONB DEFAULT '[]' NOT NULL, 
	reference_answer VARCHAR, 
	scoring_rubric VARCHAR, 
	explanation_blocks JSONB DEFAULT '[]' NOT NULL, 
	grounding VARCHAR(32) NOT NULL, 
	grounding_refs JSONB DEFAULT '{}' NOT NULL, 
	admission_state VARCHAR(32) DEFAULT 'needs_validation' NOT NULL, 
	difficulty VARCHAR(32), 
	default_score NUMERIC(8, 2) DEFAULT '10' NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (question_id, revision), 
	UNIQUE (id, question_id), 
	UNIQUE (id, subject_id), 
	FOREIGN KEY(question_id, subject_id) REFERENCES questions (id, subject_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_revision_positive CHECK (revision > 0 AND default_score > 0), 
	CONSTRAINT ck_revision_grounding CHECK (grounding IN ('uploaded_material','general_knowledge')), 
	CONSTRAINT ck_revision_admission CHECK (admission_state IN ('incomplete','needs_validation','eligible','needs_correction'))
)""",
    """CREATE TABLE material_versions (
	series_id UUID NOT NULL, 
	subject_id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	label VARCHAR(160) NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id, series_id), 
	UNIQUE (id, subject_id), 
	FOREIGN KEY(series_id, subject_id) REFERENCES material_series (id, subject_id) ON DELETE RESTRICT, 
	FOREIGN KEY(document_id, subject_id) REFERENCES documents (id, subject_id) ON DELETE RESTRICT
)""",
    """CREATE TABLE practice_items (
	session_id UUID NOT NULL, 
	subject_id UUID NOT NULL, 
	position INTEGER NOT NULL, 
	question_id UUID NOT NULL, 
	question_revision_id UUID NOT NULL, 
	fixed_max_score NUMERIC(8, 2) NOT NULL, 
	scoring_input JSONB NOT NULL, 
	knowledge_point_ids JSONB NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (session_id, position), 
	UNIQUE (session_id, question_id), 
	FOREIGN KEY(session_id, subject_id) REFERENCES practice_sessions (id, subject_id) ON DELETE RESTRICT, 
	FOREIGN KEY(question_id, subject_id) REFERENCES questions (id, subject_id) ON DELETE RESTRICT, 
	FOREIGN KEY(question_revision_id, question_id) REFERENCES question_revisions (id, question_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_practice_item_positive CHECK (position > 0 AND fixed_max_score > 0)
)""",
    """CREATE TABLE question_origins (
	question_id UUID NOT NULL, 
	subject_id UUID NOT NULL, 
	source_kind VARCHAR(32) NOT NULL, 
	document_id UUID, 
	generation_run_id UUID, 
	source_location JSONB NOT NULL, 
	answer_origin VARCHAR(32), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(question_id, subject_id) REFERENCES questions (id, subject_id) ON DELETE RESTRICT, 
	FOREIGN KEY(document_id, subject_id) REFERENCES documents (id, subject_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_origin_real_source CHECK ((source_kind='uploaded' AND document_id IS NOT NULL AND generation_run_id IS NULL) OR (source_kind='ai' AND document_id IS NULL AND generation_run_id IS NOT NULL))
)""",
    """CREATE TABLE question_revision_knowledge_points (
	revision_id UUID NOT NULL, 
	knowledge_point_id UUID NOT NULL, 
	subject_id UUID NOT NULL, 
	PRIMARY KEY (revision_id, knowledge_point_id), 
	FOREIGN KEY(revision_id, subject_id) REFERENCES question_revisions (id, subject_id) ON DELETE RESTRICT, 
	FOREIGN KEY(knowledge_point_id, subject_id) REFERENCES knowledge_points (id, subject_id) ON DELETE RESTRICT
)""",
    """CREATE TABLE practice_answers (
	practice_item_id UUID NOT NULL, 
	answer_state VARCHAR(32) NOT NULL, 
	submission_state VARCHAR(32) DEFAULT 'draft' NOT NULL, 
	choice_ids JSONB, 
	text_answer VARCHAR, 
	submitted_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (practice_item_id), 
	CONSTRAINT ck_answer_state CHECK (answer_state IN ('answered','unanswered') AND submission_state IN ('draft','submitted')), 
	CONSTRAINT ck_answer_content CHECK ((answer_state='unanswered' AND choice_ids IS NULL AND text_answer IS NULL) OR (answer_state='answered' AND ((choice_ids IS NOT NULL AND text_answer IS NULL) OR (choice_ids IS NULL AND text_answer IS NOT NULL)))), 
	FOREIGN KEY(practice_item_id) REFERENCES practice_items (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE grade_attempts (
	answer_id UUID NOT NULL, 
	round INTEGER NOT NULL, 
	validation_state VARCHAR(32) NOT NULL, 
	execution_source VARCHAR(32) NOT NULL, 
	score NUMERIC(8, 2), 
	max_score NUMERIC(8, 2), 
	reason VARCHAR NOT NULL, 
	scoring_input JSONB NOT NULL, 
	runtime_snapshot_id UUID, 
	confidence FLOAT, 
	usage JSONB, 
	error_code VARCHAR(64), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (answer_id, round), 
	UNIQUE (id, answer_id), 
	CONSTRAINT ck_grade_round CHECK (round > 0), 
	CONSTRAINT ck_grade_state CHECK (validation_state IN ('valid','failed','needs_review','outcome_unknown')), 
	CONSTRAINT ck_grade_execution CHECK (execution_source IN ('local_objective','local_unanswered','model')), 
	CONSTRAINT ck_grade_score CHECK ((validation_state='valid' AND score IS NOT NULL AND max_score IS NOT NULL AND score>=0 AND max_score>0 AND score<=max_score) OR (validation_state<>'valid' AND score IS NULL)), 
	CONSTRAINT ck_grade_runtime CHECK ((execution_source='model' AND (validation_state<>'valid' OR runtime_snapshot_id IS NOT NULL)) OR (execution_source<>'model' AND runtime_snapshot_id IS NULL AND confidence IS NULL AND usage IS NULL)), 
	CONSTRAINT ck_grade_confidence CHECK (confidence IS NULL OR (confidence>=0 AND confidence<=1)), 
	FOREIGN KEY(answer_id) REFERENCES practice_answers (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE question_revision_assets (
	revision_id UUID NOT NULL, 
	asset_id UUID NOT NULL, 
	PRIMARY KEY (revision_id, asset_id), 
	FOREIGN KEY(revision_id) REFERENCES question_revisions (id) ON DELETE RESTRICT, 
	FOREIGN KEY(asset_id) REFERENCES question_assets (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE score_disputes (
	answer_id UUID NOT NULL, 
	disputed_attempt_id UUID NOT NULL, 
	state VARCHAR(32) DEFAULT 'pending' NOT NULL, 
	reason VARCHAR NOT NULL, 
	accepted_attempt_id UUID, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(disputed_attempt_id, answer_id) REFERENCES grade_attempts (id, answer_id) ON DELETE RESTRICT, 
	FOREIGN KEY(accepted_attempt_id, answer_id) REFERENCES grade_attempts (id, answer_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_dispute_state CHECK (state IN ('pending','awaiting_acceptance','resolved')), 
	CONSTRAINT ck_dispute_acceptance CHECK ((state='resolved' AND accepted_attempt_id IS NOT NULL) OR (state<>'resolved' AND accepted_attempt_id IS NULL)), 
	FOREIGN KEY(answer_id) REFERENCES practice_answers (id) ON DELETE RESTRICT
)""",
    """ALTER TABLE questions ADD CONSTRAINT fk_question_current_revision FOREIGN KEY(id, current_revision_id) REFERENCES question_revisions (question_id, id)""",
    """ALTER TABLE material_series ADD CONSTRAINT fk_material_default_version FOREIGN KEY(default_version_id, id) REFERENCES material_versions (id, series_id)""",
    """CREATE FUNCTION personal_protect_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_TABLE_NAME='question_revisions' THEN
   IF EXISTS(SELECT 1 FROM practice_items WHERE question_revision_id=OLD.id) THEN
     RAISE EXCEPTION 'REFERENCED_REVISION_IMMUTABLE' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME IN ('question_revision_assets','question_revision_knowledge_points') THEN
   IF EXISTS(SELECT 1 FROM practice_items WHERE question_revision_id=CASE WHEN TG_OP='INSERT' THEN NEW.revision_id ELSE OLD.revision_id END) THEN
     RAISE EXCEPTION 'REFERENCED_REVISION_IMMUTABLE' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME='practice_items' THEN
   IF EXISTS(SELECT 1 FROM practice_sessions WHERE id=CASE WHEN TG_OP='INSERT' THEN NEW.session_id ELSE OLD.session_id END AND state<>'preview') THEN
     RAISE EXCEPTION 'PRACTICE_BASIS_IMMUTABLE' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME='practice_answers' THEN
   IF OLD.submission_state='submitted' THEN
     RAISE EXCEPTION 'SUBMITTED_ANSWER_IMMUTABLE' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME='grade_attempts' THEN
   RAISE EXCEPTION 'GRADE_HISTORY_IMMUTABLE' USING ERRCODE='23514';
 ELSIF TG_TABLE_NAME='material_versions' THEN
   IF TG_OP='DELETE' OR (NEW.document_id,NEW.subject_id,NEW.series_id) IS DISTINCT FROM (OLD.document_id,OLD.subject_id,OLD.series_id) THEN
     RAISE EXCEPTION 'MATERIAL_VERSION_IMMUTABLE' USING ERRCODE='23514';
   END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$""",
    """CREATE TRIGGER protect_question_revisions BEFORE UPDATE OR DELETE ON question_revisions FOR EACH ROW EXECUTE FUNCTION personal_protect_history()""",
    """CREATE TRIGGER protect_question_revision_assets BEFORE INSERT OR UPDATE OR DELETE ON question_revision_assets FOR EACH ROW EXECUTE FUNCTION personal_protect_history()""",
    """CREATE TRIGGER protect_question_revision_knowledge_points BEFORE INSERT OR UPDATE OR DELETE ON question_revision_knowledge_points FOR EACH ROW EXECUTE FUNCTION personal_protect_history()""",
    """CREATE TRIGGER protect_practice_items BEFORE INSERT OR UPDATE OR DELETE ON practice_items FOR EACH ROW EXECUTE FUNCTION personal_protect_history()""",
    """CREATE TRIGGER protect_practice_answers BEFORE UPDATE OR DELETE ON practice_answers FOR EACH ROW EXECUTE FUNCTION personal_protect_history()""",
    """CREATE TRIGGER protect_grade_attempts BEFORE UPDATE OR DELETE ON grade_attempts FOR EACH ROW EXECUTE FUNCTION personal_protect_history()""",
    """CREATE TRIGGER protect_material_versions BEFORE UPDATE OR DELETE ON material_versions FOR EACH ROW EXECUTE FUNCTION personal_protect_history()""",
    """CREATE FUNCTION personal_validate_round() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE astate text; aitem uuid; fixed numeric; accepted_round integer; disputed_round integer; latest integer; accepted_state text;
BEGIN
 IF TG_TABLE_NAME='grade_attempts' THEN
   SELECT answer_state,practice_item_id INTO astate,aitem FROM practice_answers WHERE id=NEW.answer_id FOR UPDATE;
   SELECT fixed_max_score INTO fixed FROM practice_items WHERE id=aitem;
   IF NEW.validation_state='valid' AND NEW.max_score<>fixed THEN
     RAISE EXCEPTION 'GRADE_MAX_SCORE_CHANGED' USING ERRCODE='23514';
   END IF;
   IF NEW.execution_source='local_unanswered' AND (astate<>'unanswered' OR NEW.score<>0) THEN
     RAISE EXCEPTION 'LOCAL_UNANSWERED_STATE_REQUIRED' USING ERRCODE='23514';
   END IF;
 ELSE
   PERFORM id FROM practice_answers WHERE id=NEW.answer_id FOR UPDATE;
   IF NEW.state='resolved' THEN
     SELECT round,validation_state INTO accepted_round,accepted_state FROM grade_attempts WHERE id=NEW.accepted_attempt_id AND answer_id=NEW.answer_id;
     SELECT round INTO disputed_round FROM grade_attempts WHERE id=NEW.disputed_attempt_id AND answer_id=NEW.answer_id;
     SELECT max(round) INTO latest FROM grade_attempts WHERE answer_id=NEW.answer_id;
     IF accepted_round IS NULL OR disputed_round IS NULL OR accepted_state<>'valid' OR accepted_round<>latest OR accepted_round<=disputed_round THEN
       RAISE EXCEPTION 'CURRENT_VALID_REGRADE_REQUIRED' USING ERRCODE='23514';
     END IF;
   END IF;
 END IF;
 RETURN NEW;
END $$""",
    """CREATE TRIGGER validate_grade_round BEFORE INSERT ON grade_attempts FOR EACH ROW EXECUTE FUNCTION personal_validate_round()""",
    """CREATE TRIGGER validate_dispute_round BEFORE INSERT OR UPDATE ON score_disputes FOR EACH ROW EXECUTE FUNCTION personal_validate_round()""",
    """CREATE FUNCTION personal_validate_hierarchy() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cyclic boolean;
BEGIN
 IF TG_TABLE_NAME='chapters' THEN
  IF NEW.parent_id IS NULL THEN RETURN NEW; END IF;
   WITH RECURSIVE parents AS (SELECT id,parent_id FROM chapters WHERE id=NEW.parent_id UNION SELECT c.id,c.parent_id FROM chapters c JOIN parents p ON c.id=p.parent_id) SELECT EXISTS(SELECT 1 FROM parents WHERE id=NEW.id) INTO cyclic;
 ELSIF TG_TABLE_NAME='questions' THEN
  IF NEW.canonical_id IS NULL THEN RETURN NEW; END IF;
   WITH RECURSIVE parents AS (SELECT id,canonical_id FROM questions WHERE id=NEW.canonical_id UNION SELECT q.id,q.canonical_id FROM questions q JOIN parents p ON q.id=p.canonical_id) SELECT EXISTS(SELECT 1 FROM parents WHERE id=NEW.id) INTO cyclic;
 END IF;
 IF cyclic THEN RAISE EXCEPTION 'IDENTITY_CYCLE' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$""",
    """CREATE TRIGGER validate_personal_chapter BEFORE INSERT OR UPDATE ON chapters FOR EACH ROW EXECUTE FUNCTION personal_validate_hierarchy()""",
    """CREATE TRIGGER validate_personal_canonical BEFORE INSERT OR UPDATE ON questions FOR EACH ROW EXECUTE FUNCTION personal_validate_hierarchy()""",
)


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError("PERSONAL_HISTORY_DOWNGRADE_REQUIRES_EXPLICIT_REVIEW")
