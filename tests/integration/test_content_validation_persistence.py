"""T163 real PostgreSQL locks, revision freshness and durable constraints. TCR §17."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.database import create_database_engine
from backend.app.models import AgentRun, Base, Question, QuestionValidationResult
from backend.app.schemas.content_validation import (
    ValidationEvidence,
    ValidationInputRefs,
    ValidationProvenance,
)
from backend.app.services.content_validation_service import ContentValidationService
from tests.contract.test_retrieval_contract import _seed_course, _vector
from tests.unit.services.test_content_validation_service import result


@pytest.fixture
def pg_case(tmp_path):
    engine = create_database_engine(connect_timeout=3)
    schema = "t163_validation_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(isolated)
        with Session(isolated, expire_on_commit=False) as session:
            course, _kb, doc, chunks = _seed_course(
                session,
                name=uuid4().hex[:8],
                vectors=[_vector(0)],
                contents=["Actual teaching evidence"],
            )
            question = Question(
                course_id=course.id,
                created_by=doc.uploaded_by,
                type="SHORT_ANSWER",
                content="Real fixture condition",
                reference_answer="Reference condition",
                scoring_rubric="condition: 1 point",
                score=Decimal("1.00"),
                status="Pending Review",
            )
            session.add(question)
            session.commit()
            chunk = chunks[0]
            refs = ValidationInputRefs(
                fields=[
                    "type",
                    "content",
                    "options",
                    "reference_answer",
                    "scoring_rubric",
                    "analysis",
                    "score",
                ],
                evidence=[
                    ValidationEvidence(
                        evidence_id=uuid4(),
                        kind="chunk",
                        source_id=chunk.id,
                        source_data={
                            "chunk_id": str(chunk.id),
                            "document_id": str(doc.id),
                            "course_id": str(course.id),
                            "source_file": doc.original_filename,
                            "location": chunk.chunk_metadata["location"],
                            "content_snapshot": chunk.content,
                        },
                    )
                ],
            )
            yield isolated, question.id, doc.uploaded_by, refs, tmp_path / "owned-files"
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_two_preloaded_sessions_allocate_distinct_global_rounds(pg_case):
    engine, question_id, teacher_id, refs, root = pg_case
    barrier = Barrier(2)

    def start():
        with Session(engine, expire_on_commit=False) as session:
            session.get(Question, question_id)
            barrier.wait(timeout=15)
            report = ContentValidationService(session, root=root).start_validation(
                question_id,
                actor_id=teacher_id,
                input_refs=refs,
                executor_name="fixture",
            )
            assert (
                not session.in_transaction()
            ), "The provider must receive control after the startup lock is released."
            return report.run_no

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start) for _ in range(2)]
        assert sorted(future.result(timeout=25) for future in futures) == [1, 2]


def test_preloaded_identity_reads_current_revision_under_question_lock(pg_case):
    engine, question_id, teacher_id, refs, root = pg_case
    with Session(engine, expire_on_commit=False) as stale:
        loaded = stale.get(Question, question_id)
        assert loaded.validation_revision == 0
        with Session(engine) as fresh:
            current = fresh.get(Question, question_id)
            current.content = "Actually changed input"
            current.validation_revision += 1
            fresh.commit()
        report = ContentValidationService(stale, root=root).start_validation(
            question_id, actor_id=teacher_id, input_refs=refs, executor_name="fixture"
        )
        assert report.input_revision == 1 and loaded.content == "Actually changed input"


@pytest.mark.parametrize(
    "changes",
    [
        {"run_no": 0},
        {"input_revision": -1},
        {"outcome": "unknown"},
        {"input_refs": []},
        {"provenance": []},
        {"manual_dispositions": {}},
        {"completed_at": datetime.now(UTC)},
    ],
)
def test_database_rejects_bad_report_state_json_and_counters(pg_case, changes):
    engine, question_id, teacher_id, refs, root = pg_case
    with Session(engine, expire_on_commit=False) as session:
        service = ContentValidationService(session, root=root)
        report = service.start_validation(
            question_id, actor_id=teacher_id, input_refs=refs, executor_name="fixture"
        )
        with pytest.raises(IntegrityError):
            session.execute(
                update(QuestionValidationResult)
                .where(QuestionValidationResult.id == report.id)
                .values(**changes)
            )
            session.commit()
        session.rollback()
        preserved = session.get(QuestionValidationResult, report.id)
        assert (
            preserved.outcome == "running"
            and preserved.run_no == 1
            and preserved.input_revision == 0
        )


def test_trace_cleanup_only_nulls_pointer_durable_provenance_survives(pg_case):
    engine, question_id, teacher_id, refs, root = pg_case
    with Session(engine, expire_on_commit=False) as session:
        trace = AgentRun(
            agent_type="fixture_validation",
            request_id=uuid4().hex,
            user_id=teacher_id,
            status="success",
        )
        session.add(trace)
        session.commit()
        service = ContentValidationService(session, root=root)
        started = service.start_validation(
            question_id,
            actor_id=teacher_id,
            input_refs=refs,
            executor_name="fixture_validation",
            executor_kind="agent",
            agent_run_id=trace.id,
        )
        done = service.finish_validation(
            started.id,
            actor_id=teacher_id,
            output=result(refs),
            provenance=ValidationProvenance(
                agent_type="fixture_validation",
                provider_name="fixture",
                model="fixture-model",
                prompt_version="fixture-1",
            ),
        )
        assert done.agent_run_id == trace.id and done.outcome == "passed"
        session.delete(trace)
        session.commit()
        session.expire_all()
        durable = service.get_validation(question_id, started.id, actor_id=teacher_id)
        assert (
            durable.agent_run_id is None and durable.provenance.model == "fixture-model"
        )
        assert (
            durable.requested_by == teacher_id
            and durable.created_at.utcoffset().total_seconds() == 0
        )
