"""Feature 002 foundation: real owned PG, migrations and historical invariants."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from tests.postgres_helpers import isolated_postgres_schema

ROOT = Path(__file__).resolve().parents[2]
OLD = "0024_embedding_index_identity"
NEW = "0025_personal_foundation"
TABLES = {
    "subjects",
    "knowledge_points",
    "chapter_knowledge_points",
    "material_series",
    "material_versions",
    "question_revisions",
    "question_origins",
    "question_revision_knowledge_points",
    "question_revision_assets",
    "practice_sessions",
    "practice_items",
    "practice_answers",
    "grade_attempts",
    "score_disputes",
}


def migrate(engine, revision):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    # Migrations must not replace pytest handlers or disable SDK loggers.
    config.config_file_name = None
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


@pytest.fixture
def pg():
    with isolated_postgres_schema() as (engine, schema):
        migrate(engine, NEW)
        yield engine, schema


def sql(connection, statement, **values):
    return connection.execute(text(statement), values)


def seed(engine):
    ids = {
        key: uuid4()
        for key in (
            "subject",
            "kp",
            "chapter",
            "document",
            "series",
            "version",
            "question",
            "revision",
            "session",
            "item",
            "answer",
            "attempt",
        )
    }
    with engine.begin() as c:
        sql(c, "INSERT INTO subjects(id,name) VALUES (:id,'math')", id=ids["subject"])
        sql(
            c,
            "INSERT INTO knowledge_points(id,subject_id,name) VALUES (:id,:s,'algebra')",
            id=ids["kp"],
            s=ids["subject"],
        )
        sql(
            c,
            "INSERT INTO chapters(id,subject_id,title,sections) VALUES (:id,:s,'one','[]')",
            id=ids["chapter"],
            s=ids["subject"],
        )
        sql(
            c,
            "INSERT INTO chapter_knowledge_points(chapter_id,knowledge_point_id,subject_id) VALUES (:c,:k,:s)",
            c=ids["chapter"],
            k=ids["kp"],
            s=ids["subject"],
        )
        sql(
            c,
            "INSERT INTO documents(id,subject_id,original_filename,file_format,purpose,status,retryable) VALUES (:id,:s,'paper.txt','txt','paper_source','Uploaded',false)",
            id=ids["document"],
            s=ids["subject"],
        )
        sql(
            c,
            "INSERT INTO material_series(id,subject_id,name) VALUES (:id,:s,'book')",
            id=ids["series"],
            s=ids["subject"],
        )
        sql(
            c,
            "INSERT INTO material_versions(id,series_id,subject_id,document_id,label) VALUES (:id,:series,:s,:d,'v1')",
            id=ids["version"],
            series=ids["series"],
            s=ids["subject"],
            d=ids["document"],
        )
        sql(
            c,
            "UPDATE material_series SET default_version_id=:v WHERE id=:id",
            v=ids["version"],
            id=ids["series"],
        )
        sql(
            c,
            "INSERT INTO questions(id,subject_id,knowledge_points,order_preserved,status) VALUES (:id,:s,'[]',true,'Draft')",
            id=ids["question"],
            s=ids["subject"],
        )
        sql(
            c,
            "INSERT INTO question_revisions(id,question_id,subject_id,revision,question_type,stem_blocks,options,reference_answer,grounding,admission_state,default_score) VALUES (:id,:q,:s,1,'single_choice','[{\"type\": \"text\",\"body\": \"x=1?\"}]','[]','A','general_knowledge','eligible',10)",
            id=ids["revision"],
            q=ids["question"],
            s=ids["subject"],
        )
        sql(
            c,
            "UPDATE questions SET current_revision_id=:r WHERE id=:q",
            r=ids["revision"],
            q=ids["question"],
        )
        sql(
            c,
            "INSERT INTO question_revision_knowledge_points(revision_id,knowledge_point_id,subject_id) VALUES (:r,:k,:s)",
            r=ids["revision"],
            k=ids["kp"],
            s=ids["subject"],
        )
        sql(
            c,
            "INSERT INTO question_origins(id,question_id,subject_id,source_kind,document_id,source_location) VALUES (:id,:q,:s,'uploaded',:d,'{\"paragraph\": 1}')",
            id=uuid4(),
            q=ids["question"],
            s=ids["subject"],
            d=ids["document"],
        )
        sql(
            c,
            "INSERT INTO practice_sessions(id,subject_id,state,conditions) VALUES (:id,:s,'preview','{}')",
            id=ids["session"],
            s=ids["subject"],
        )
        sql(
            c,
            "INSERT INTO practice_items(id,session_id,subject_id,position,question_id,question_revision_id,fixed_max_score,scoring_input,knowledge_point_ids) VALUES (:id,:session,:s,1,:q,:r,10,'{\"reference_answer\": \"A\"}','[]')",
            id=ids["item"],
            session=ids["session"],
            s=ids["subject"],
            q=ids["question"],
            r=ids["revision"],
        )
        sql(
            c,
            "UPDATE practice_sessions SET state='in_progress' WHERE id=:id",
            id=ids["session"],
        )
        sql(
            c,
            "INSERT INTO practice_answers(id,practice_item_id,answer_state,choice_ids,submission_state) VALUES (:id,:i,'answered','[\"A\"]','draft')",
            id=ids["answer"],
            i=ids["item"],
        )
        sql(
            c,
            "INSERT INTO grade_attempts(id,answer_id,round,validation_state,execution_source,score,max_score,reason,scoring_input) VALUES (:id,:a,1,'valid','local_objective',0,10,'wrong','{}')",
            id=ids["attempt"],
            a=ids["answer"],
        )
    return ids


@pytest.mark.parametrize("upgrade_old", [False, True])
def test_fresh_and_old_head_upgrade_preserve_existing_rows(upgrade_old):
    with isolated_postgres_schema() as (engine, schema):
        legacy = uuid4()
        if upgrade_old:
            migrate(engine, OLD)
            with engine.begin() as c:
                sql(
                    c,
                    "INSERT INTO users(id,username,email,password_hash,is_active) VALUES (:id,'legacy','legacy@example.invalid','synthetic',true)",
                    id=legacy,
                )
        migrate(engine, NEW)
        migrate(engine, NEW)
        with engine.connect() as c:
            assert c.scalar(text("SELECT current_schema()")) == schema
            assert c.scalar(text("SELECT version_num FROM alembic_version")) == NEW
            assert TABLES <= set(inspect(c).get_table_names(schema=schema))
            assert c.scalar(text("SELECT count(*) FROM users")) == int(upgrade_old)
            if upgrade_old:
                assert (
                    sql(
                        c, "SELECT password_hash FROM users WHERE id=:id", id=legacy
                    ).scalar_one()
                    == "synthetic"
                )
        ids = seed(engine)
        with engine.connect() as c:
            assert (
                sql(
                    c,
                    "SELECT subject_id FROM questions WHERE id=:id",
                    id=ids["question"],
                ).scalar_one()
                == ids["subject"]
            )


def test_stable_knowledge_and_material_history(pg):
    engine, _ = pg
    ids = seed(engine)
    version2 = uuid4()
    with engine.begin() as c:
        sql(c, "UPDATE knowledge_points SET name='renamed' WHERE id=:id", id=ids["kp"])
        sql(
            c,
            "INSERT INTO material_versions(id,series_id,subject_id,document_id,label) VALUES (:id,:series,:s,:d,'v2')",
            id=version2,
            series=ids["series"],
            s=ids["subject"],
            d=ids["document"],
        )
        sql(
            c,
            "UPDATE material_series SET default_version_id=:v WHERE id=:id",
            v=version2,
            id=ids["series"],
        )
    with engine.connect() as c:
        assert (
            sql(
                c,
                "SELECT knowledge_point_id FROM question_revision_knowledge_points WHERE revision_id=:r",
                r=ids["revision"],
            ).scalar_one()
            == ids["kp"]
        )
        assert (
            sql(
                c, "SELECT label FROM material_versions WHERE id=:id", id=ids["version"]
            ).scalar_one()
            == "v1"
        )


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE question_revisions SET reference_answer='B' WHERE id=:revision",
        "UPDATE practice_items SET fixed_max_score=20 WHERE id=:item",
        "DELETE FROM question_revisions WHERE id=:revision",
        "UPDATE grade_attempts SET score=8 WHERE id=:attempt",
    ],
)
def test_frozen_basis_and_grade_history_cannot_be_overwritten(pg, change):
    engine, _ = pg
    ids = seed(engine)
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text(change), ids)


def test_multiple_sources_and_grade_rounds_preserve_old_facts(pg):
    engine, _ = pg
    ids = seed(engine)
    with engine.begin() as c:
        sql(
            c,
            "INSERT INTO question_origins(id,question_id,subject_id,source_kind,document_id,source_location) VALUES (:id,:q,:s,'uploaded',:d,'{\"paragraph\": 2}')",
            id=uuid4(),
            q=ids["question"],
            s=ids["subject"],
            d=ids["document"],
        )
        sql(
            c,
            "INSERT INTO grade_attempts(id,answer_id,round,validation_state,execution_source,score,max_score,reason,scoring_input) VALUES (:id,:a,2,'valid','local_objective',10,10,'corrected','{}')",
            id=uuid4(),
            a=ids["answer"],
        )
    with engine.connect() as c:
        assert (
            sql(
                c,
                "SELECT count(*) FROM question_origins WHERE question_id=:q",
                q=ids["question"],
            ).scalar_one()
            == 2
        )
        assert list(
            sql(
                c,
                "SELECT score FROM grade_attempts WHERE answer_id=:a ORDER BY round",
                a=ids["answer"],
            ).scalars()
        ) == [0, 10]


def test_cross_subject_relation_and_failed_transaction_rollback(pg):
    engine, _ = pg
    ids = seed(engine)
    other = uuid4()
    invalid = uuid4()
    with engine.begin() as c:
        sql(c, "INSERT INTO subjects(id,name) VALUES (:id,'other')", id=other)
    with pytest.raises(DBAPIError), engine.begin() as c:
        sql(
            c,
            "INSERT INTO knowledge_points(id,subject_id,name) VALUES (:id,:s,'rollback')",
            id=invalid,
            s=other,
        )
        sql(
            c,
            "INSERT INTO question_revision_knowledge_points(revision_id,knowledge_point_id,subject_id) VALUES (:r,:k,:s)",
            r=ids["revision"],
            k=invalid,
            s=other,
        )
    with engine.connect() as c:
        assert (
            sql(
                c, "SELECT count(*) FROM knowledge_points WHERE id=:id", id=invalid
            ).scalar_one()
            == 0
        )


def test_unanswered_valid_zero_and_failed_grading_are_distinct(pg):
    engine, _ = pg
    ids = seed(engine)
    with engine.begin() as c:
        sql(
            c,
            "UPDATE practice_answers SET answer_state='unanswered',choice_ids=NULL WHERE id=:a",
            a=ids["answer"],
        )
        sql(
            c,
            "INSERT INTO grade_attempts(id,answer_id,round,validation_state,execution_source,score,max_score,reason,scoring_input) VALUES (:id,:a,2,'valid','local_unanswered',0,10,'blank','{}')",
            id=uuid4(),
            a=ids["answer"],
        )
        sql(
            c,
            "INSERT INTO grade_attempts(id,answer_id,round,validation_state,execution_source,reason,scoring_input,error_code) VALUES (:id,:a,3,'failed','model','failed','{}','SCHEMA_FAILED')",
            id=uuid4(),
            a=ids["answer"],
        )
    with engine.connect() as c:
        rows = sql(
            c,
            "SELECT validation_state,score FROM grade_attempts WHERE answer_id=:a ORDER BY round",
            a=ids["answer"],
        ).all()
        assert rows == [("valid", 0), ("valid", 0), ("failed", None)]
        assert (
            sql(
                c,
                "SELECT answer_state FROM practice_answers WHERE id=:id",
                id=ids["answer"],
            ).scalar_one()
            == "unanswered"
        )


def test_dispute_acceptance_rejects_stale_round(pg):
    engine, _ = pg
    ids = seed(engine)
    dispute = uuid4()
    with engine.begin() as c:
        sql(
            c,
            "INSERT INTO grade_attempts(id,answer_id,round,validation_state,execution_source,score,max_score,reason,scoring_input) VALUES (:id,:a,2,'valid','local_objective',10,10,'new','{}')",
            id=uuid4(),
            a=ids["answer"],
        )
    with pytest.raises(DBAPIError), engine.begin() as c:
        sql(
            c,
            "INSERT INTO score_disputes(id,answer_id,disputed_attempt_id,state,reason,accepted_attempt_id) VALUES (:id,:a,:old,'resolved','review',:old)",
            id=dispute,
            a=ids["answer"],
            old=ids["attempt"],
        )


def test_missing_legacy_required_fields_still_rejected(pg):
    engine, _ = pg
    with pytest.raises(DBAPIError) as error, engine.begin() as c:
        sql(
            c,
            "INSERT INTO questions(id,knowledge_points,order_preserved,status) VALUES (:id,'[]',true,'Draft')",
            id=uuid4(),
        )
    assert error.value.orig.diag.constraint_name == "ck_question_domain"


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE practice_answers SET submission_state='submitted' WHERE id=:answer; UPDATE practice_answers SET choice_ids='[\"B\"]' WHERE id=:answer",
        "INSERT INTO question_revision_knowledge_points(revision_id,knowledge_point_id,subject_id) VALUES (:revision,:new_kp,:subject)",
        "INSERT INTO practice_items(id,session_id,subject_id,position,question_id,question_revision_id,fixed_max_score,scoring_input,knowledge_point_ids) VALUES (:new_item,:session,:subject,2,:question,:revision,10,'{}','[]')",
    ],
)
def test_started_history_rejects_late_answer_basis_and_item_changes(pg, change):
    engine, _ = pg
    ids = seed(engine)
    ids["new_kp"] = uuid4()
    ids["new_item"] = uuid4()
    with engine.begin() as c:
        sql(
            c,
            "INSERT INTO knowledge_points(id,subject_id,name) VALUES (:id,:s,'new')",
            id=ids["new_kp"],
            s=ids["subject"],
        )
    with pytest.raises(DBAPIError) as error, engine.begin() as c:
        for statement in change.split(";"):
            c.execute(text(statement), ids)
    expected = (
        "SUBMITTED_ANSWER_IMMUTABLE"
        if change.startswith("UPDATE practice_answers")
        else "REFERENCED_REVISION_IMMUTABLE"
        if "INSERT INTO question_revision_knowledge_points" in change
        else "PRACTICE_BASIS_IMMUTABLE"
    )
    assert expected in str(error.value.orig)


def test_current_head_failed_migration_never_reports_ready_and_retry_is_atomic(
    monkeypatch,
):
    from sqlalchemy import event

    from backend.app.services.settings import readiness
    from tests.unit.settings_helpers import build_test_settings

    with isolated_postgres_schema() as (engine, schema):
        migrate(engine, OLD)
        settings = build_test_settings(
            database_url=engine.url.update_query_dict(
                {"options": f"-csearch_path={schema},public"}
            ).render_as_string(hide_password=False)
        )

        def fail(connection, cursor, statement, parameters, context, executemany):
            if "CREATE TABLE material_versions" in statement:
                raise RuntimeError("SYNTHETIC_MIGRATION_INTERRUPTION")

        event.listen(engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(RuntimeError, match="SYNTHETIC_MIGRATION_INTERRUPTION"):
                migrate(engine, "head")
        finally:
            event.remove(engine, "before_cursor_execute", fail)
        with engine.connect() as c:
            assert c.scalar(text("SELECT version_num FROM alembic_version")) == OLD
            assert "subjects" not in inspect(c).get_table_names(schema=schema)

        def report():
            inputs = readiness.ReadinessInputs(
                business_settings=settings,
                setup_ready=True,
                assembly="assembled",
                recovery="completed",
                expected_migration_head=readiness.discover_migration_head(),
                credential_backend_available=True,
            )
            probes = readiness.DependencyProbes(
                database=readiness.probe_database,
                redis=lambda _: readiness.OK,
                storage=lambda _: readiness.OK,
            )
            return readiness.evaluate_readiness(inputs, probes)

        assert report().business_prerequisites.checks.migration.status == "failed"
        assert report().business.ready is False
        migrate(engine, "head")
        assert report().business_prerequisites.checks.migration.status == "ok"
        assert report().business.ready is True
        with engine.connect() as c:
            assert "operation_runs" in inspect(c).get_table_names(schema=schema)


def test_real_pg_operation_retains_snapshot_references_and_never_resends_after_interrupt(
    pg, tmp_path
):
    import asyncio
    import json

    import httpx
    from sqlalchemy.orm import sessionmaker

    from backend.app.services.learning_operations import LearningOperations, StepPlan
    from backend.app.services.settings.management import BusinessRunFacts
    from backend.app.services.settings.runtime_snapshot import SnapshotReferenceIndex
    from tests.contract.test_learning_operations import invoke
    from tests.support.runtime_settings_fakes import settings_harness
    from tests.unit.settings_helpers import build_test_settings

    engine, schema = pg
    migrate(engine, "head")
    harness = settings_harness(tmp_path)
    asyncio.run(harness.activate_model("test-model-a", with_credential=True))
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model-a",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"value": 1}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    service = LearningOperations(
        sessions=sessionmaker(engine, expire_on_commit=False),
        config=harness.service,
        transport=httpx.MockTransport(handler),
    )
    try:
        operation = service.prepare(
            purpose="generation",
            idempotency_key=uuid4().hex,
            steps=[
                StepPlan(
                    step_id="main",
                    task_kind="question_generation",
                    messages=[{"role": "user", "content": "JSON"}],
                    request_budget=1,
                )
            ],
        )
        service.confirm(operation.id, preview_digest=operation.preview_digest)

        async def interrupted(runtime, step):
            await invoke(runtime, step)
            raise SystemExit("SYNTHETIC_PROCESS_INTERRUPTION")

        with pytest.raises(SystemExit):
            asyncio.run(service.execute(operation.id, invoke=interrupted))
        assert service.snapshots.active_lease_count == 0
        reopened = LearningOperations(
            sessions=service.sessions,
            config=harness.service,
            transport=service.transport,
        )
        try:
            state = asyncio.run(reopened.execute(operation.id, invoke=invoke))
            assert state.actual_requests == 1 and len(calls) == 1
            assert state.steps[0]["state"] == "outcome_unknown"
            settings = build_test_settings(
                database_url=engine.url.update_query_dict(
                    {"options": f"-csearch_path={schema},public"}
                ).render_as_string(hide_password=False)
            )
            facts = BusinessRunFacts(settings=lambda: settings, configured=lambda: True)
            ref = harness.provider_reference
            assert SnapshotReferenceIndex(reopened.snapshots, facts.bindings)(
                ref, ref.version
            ) == ("snapshot",)
            snapshot = reopened.snapshots.load(operation.runtime_snapshot_id)
            assert snapshot.credential_refs and snapshot.embedding_identity is not None
        finally:
            asyncio.run(reopened.aclose())
    finally:
        asyncio.run(service.aclose())
        harness.close()
