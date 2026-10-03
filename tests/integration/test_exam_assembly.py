"""T172 real PostgreSQL assembly facts, intent persistence, and eligibility (TCR §27)."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.domain.enums import (
    ExamStatus,
    QuestionSourceType,
    QuestionStatus,
    QuestionType,
    UserRole,
)
from backend.app.models import Course, Exam, Question, QuestionValidationResult
from backend.app.schemas.exam_assembly import AssemblyRequest
from backend.app.services.exam_assembly_service import (
    AssemblyError,
    ExamAssemblyService,
)
from backend.app.services.exam_service import ExamService
from backend.app.services.question_service import QuestionService
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.support.question_validation_fixtures import persist_current_semantic_pass
from tests.unit.services.test_submission_service import add_user


@pytest.fixture
def database() -> Any:
    with isolated_postgres_engine() as engine:
        with Session(engine) as session:
            teacher = add_user(
                session,
                UserRole.TEACHER,
                username="assembly-owner",
                email="assembly-owner@example.com",
            )
            course = Course(name="Assembly course", creator=teacher)
            session.add(course)
            session.commit()
            actor, course_id = teacher.id, course.id
            ids = []
            for index, (kind, score, labels) in enumerate(
                [
                    (QuestionType.SHORT_ANSWER, "2.00", ["K1"]),
                    (QuestionType.SHORT_ANSWER, "3.00", ["K2"]),
                    (QuestionType.SINGLE_CHOICE, "3.00", ["K1", "K2"]),
                    (QuestionType.TRUE_FALSE, "2.00", ["K3"]),
                ]
            ):
                question = Question(
                    course_id=course_id,
                    type=kind,
                    content=f"Controlled question {index}",
                    options=(
                        {"A": "First", "B": "Second"}
                        if kind == QuestionType.SINGLE_CHOICE
                        else None
                    ),
                    reference_answer=(
                        "A"
                        if kind == QuestionType.SINGLE_CHOICE
                        else (
                            "true"
                            if kind == QuestionType.TRUE_FALSE
                            else "A controlled answer."
                        )
                    ),
                    scoring_rubric=f"Correct answer receives {score} points.",
                    analysis="Controlled explanation.",
                    score=Decimal(score),
                    knowledge_points=labels,
                    status=QuestionStatus.PENDING_REVIEW,
                    source_type=QuestionSourceType.MANUAL,
                    created_by=actor,
                )
                session.add(question)
                session.commit()
                identity = question.id
                persist_current_semantic_pass(session, identity, actor)
                QuestionService(session).update_question_status(
                    identity, QuestionStatus.APPROVED, teacher_id=actor
                )
                ids.append(identity)
            summary = ExamService(session).create_exam(
                course_id, "Assembly exam", question_ids=[ids[1]], teacher_id=actor
            )
            exam = session.get(Exam, UUID(str(summary.id)))
            assert exam is not None
            confirm_synthetic_exam_basis(session, exam)
            session.commit()
            yield_info = {
                "actor": actor,
                "course": course_id,
                "exam": exam.id,
                "questions": ids,
            }
        yield engine, yield_info


def request(info: dict[str, Any], **changes: Any) -> AssemblyRequest:
    raw = {
        "course_id": info["course"],
        "question_count": 2,
        "type_distribution": [
            {"question_type": "SHORT_ANSWER", "count": 1},
            {"question_type": "SINGLE_CHOICE", "count": 1},
        ],
        "knowledge_coverage": [
            {"knowledge_point": "K1", "min_questions": 1},
            {"knowledge_point": "K2", "min_questions": 1},
        ],
        "total_score": "5.00",
    }
    raw.update(changes)
    return AssemblyRequest.model_validate(raw)


def facts(session: Session, exam_id: UUID) -> list[tuple[Any, ...]]:
    session.expire_all()
    exam = session.get(Exam, exam_id)
    assert exam is not None
    return [
        (
            link.id,
            link.question_id,
            link.order_index,
            link.score,
            link.base_score,
            deepcopy(link.published_knowledge_points),
            deepcopy(link.scoring_basis),
        )
        for link in exam.exam_question_links
    ]


def test_exact_constraints_and_explicit_scores_survive_fresh_read(
    database: Any,
) -> None:
    engine, info = database
    with Session(engine) as session:
        result = ExamAssemblyService(session).assemble(
            info["exam"], request(info), actor_id=info["actor"]
        )
        assert {item.question_id for item in result.exam_questions} == {
            info["questions"][0],
            info["questions"][2],
        }
        assert [item.order_index for item in result.exam_questions] == [1, 2]
        assert result.total_score == Decimal("5.00")
        assert all(condition.satisfied for condition in result.conditions)
        assert all(
            item.score is not None
            and item.base_score is None
            and item.scoring_basis is None
            for item in result.exam_questions
        )
        assert {item.code for item in result.publication_checks} >= {
            "EXAM_SCORING_BASIS_MISSING"
        }
        assert result.assembly_constraints.recorded_by == info["actor"]
        assert result.assembly_constraints.recorded_at.utcoffset().total_seconds() == 0
    with Session(engine) as session:
        preview = ExamAssemblyService(session).preview(
            info["exam"], actor_id=info["actor"]
        )
        assert preview.model_dump(mode="json") == result.model_dump(mode="json")
        assert session.get(Question, info["questions"][0]).score == Decimal("2.00")


def test_overrides_apply_only_to_selected_questions_and_do_not_mutate_bank(
    database: Any,
) -> None:
    engine, info = database
    payload = request(
        info,
        total_score="7.00",
        score_overrides=[
            {"question_id": info["questions"][0], "score": "4.00"},
            {"question_id": info["questions"][3], "score": "99.00"},
        ],
    )
    with Session(engine) as session:
        result = ExamAssemblyService(session).assemble(
            info["exam"], payload, actor_id=info["actor"]
        )
        assert {item.question_id for item in result.exam_questions} == {
            info["questions"][0],
            info["questions"][2],
        }
        assert result.total_score == Decimal("7.00")
        assert session.get(Question, info["questions"][0]).score == Decimal("2.00")
        assert session.get(Question, info["questions"][3]).score == Decimal("2.00")


@pytest.mark.parametrize(
    "changes",
    [
        {
            "question_count": 4,
            "type_distribution": [{"question_type": "SINGLE_CHOICE", "count": 4}],
            "knowledge_coverage": [],
            "total_score": "12.00",
        },
        {"knowledge_coverage": [{"knowledge_point": "Unknown", "min_questions": 1}]},
        {"total_score": "100.00"},
        {"total_score": "5.50"},
    ],
)
def test_conflict_preserves_all_facts_but_records_latest_intent(
    database: Any, changes: dict[str, Any]
) -> None:
    engine, info = database
    with Session(engine) as session:
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session).assemble(
                info["exam"], request(info, **changes), actor_id=info["actor"]
            )
        detail = error.value.as_detail()
        assert detail["code"] == "EXAM_ASSEMBLY_UNSATISFIED"
        assert detail["intent_saved"] is True
        assert detail["reason"] == "definite_conflict"
        assert detail["gaps"]
        assert facts(session, info["exam"]) == before
    with Session(engine) as session:
        preview = ExamAssemblyService(session).preview(
            info["exam"], actor_id=info["actor"]
        )
        assert preview.assembly_constraints.request == request(info, **changes)
        assert any(not item.satisfied for item in preview.conditions)
        assert preview.total_score == Decimal("3.00")


def test_bounded_search_distinguishes_no_result_from_mathematical_conflict(
    database: Any,
) -> None:
    engine, info = database
    with Session(engine) as session:
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session, search_limit=1).assemble(
                info["exam"], request(info), actor_id=info["actor"]
            )
        detail = error.value.as_detail()
        assert (
            detail["reason"] == "strategy_not_found" and detail["intent_saved"] is True
        )
        assert facts(session, info["exam"]) == before


@pytest.mark.parametrize(
    "case", ["course", "override", "published", "forbidden", "missing"]
)
def test_rejected_admission_does_not_save_intent(database: Any, case: str) -> None:
    engine, info = database
    with Session(engine) as session:
        payload = request(info)
        actor, exam_id = info["actor"], info["exam"]
        if case == "course":
            payload = request(info, course_id=uuid4())
        elif case == "override":
            payload = request(
                info, score_overrides=[{"question_id": uuid4(), "score": "2.00"}]
            )
        elif case == "published":
            session.get(Exam, exam_id).status = ExamStatus.PUBLISHED
            session.commit()
        elif case == "forbidden":
            other = add_user(
                session,
                UserRole.TEACHER,
                username="other-assembly",
                email="other-assembly@example.com",
            )
            session.commit()
            actor = other.id
        else:
            exam_id = uuid4()
        with pytest.raises(AssemblyError):
            ExamAssemblyService(session).assemble(exam_id, payload, actor_id=actor)
        session.rollback()
        assert session.get(Exam, info["exam"]).assembly_constraints is None


@pytest.mark.parametrize(
    "case", ["stale", "missing", "failed", "bad_source", "pending"]
)
def test_current_semantic_evidence_is_required_for_approved_candidates(
    database: Any, case: str
) -> None:
    engine, info = database
    with Session(engine) as session:
        question = session.get(Question, info["questions"][2])
        report = session.scalars(
            select(QuestionValidationResult).where(
                QuestionValidationResult.question_id == question.id
            )
        ).one()
        if case == "stale":
            question.validation_revision += 1
        elif case == "missing":
            session.delete(report)
        elif case == "failed":
            report.outcome = "failed"
        elif case == "bad_source":
            refs = deepcopy(report.input_refs)
            refs["evidence"][0]["source_data"][
                "content_snapshot"
            ] = "not the actual saved evidence"
            report.input_refs = refs
        else:
            report.manual_dispositions = [
                {
                    "id": str(uuid4()),
                    "input_revision": question.validation_revision,
                    "handled_by": str(info["actor"]),
                    "handled_at": datetime.now(UTC).isoformat(),
                    "action": "provide_evidence",
                    "reason": "Synthetic pending evidence",
                    "check_kind": "answer_correctness",
                    "evidence_refs": [report.input_refs["evidence"][0]["evidence_id"]],
                    "issue_ids": [],
                    "additional_evidence": [],
                    "revision_comment_id": None,
                }
            ]
        session.commit()
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session).assemble(
                info["exam"], request(info), actor_id=info["actor"]
            )
        assert error.value.code == "EXAM_ASSEMBLY_UNSATISFIED"
        assert error.value.as_detail()["intent_saved"] is True
        assert facts(session, info["exam"]) == before


def test_fact_write_failure_rolls_back_savepoint_and_commits_intent(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, info = database
    with Session(engine) as session:
        before = facts(session, info["exam"])
        service = ExamAssemblyService(session)
        original = service._replace_questions

        def fail_after_replace(*args: Any, **kwargs: Any) -> None:
            original(*args, **kwargs)
            raise SQLAlchemyError("controlled association failure")

        monkeypatch.setattr(service, "_replace_questions", fail_after_replace)
        with pytest.raises(AssemblyError) as error:
            service.assemble(info["exam"], request(info), actor_id=info["actor"])
        assert error.value.code == "EXAM_ASSEMBLY_FAILED"
        assert error.value.as_detail()["intent_saved"] is True
        assert facts(session, info["exam"]) == before
        assert session.get(Exam, info["exam"]).assembly_constraints is not None


def test_commit_acknowledgement_failure_reports_unknown_and_fresh_read_is_truth(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, info = database
    with Session(engine) as session:
        commit = session.commit

        def lost_ack() -> None:
            commit()
            raise SQLAlchemyError("controlled lost commit acknowledgement")

        monkeypatch.setattr(session, "commit", lost_ack)
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session).assemble(
                info["exam"],
                request(info, total_score="100.00"),
                actor_id=info["actor"],
            )
        assert error.value.code == "EXAM_ASSEMBLY_UNSATISFIED"
        assert error.value.as_detail()["intent_saved"] is None
        assert error.value.as_detail()["intent_save_error"]
    with Session(engine) as session:
        assert (
            session.get(Exam, info["exam"]).assembly_constraints["request"][
                "total_score"
            ]
            == "100.00"
        )


def test_corrupt_persisted_intent_is_not_treated_as_missing(database: Any) -> None:
    engine, info = database
    with Session(engine) as session:
        session.execute(
            text("UPDATE exams SET assembly_constraints = '{}'::jsonb WHERE id = :id"),
            {"id": info["exam"]},
        )
        session.commit()
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session).preview(info["exam"], actor_id=info["actor"])
        assert error.value.code == "EXAM_ASSEMBLY_CONSTRAINTS_INVALID"


def test_missing_real_image_excludes_candidate_without_rolling_back_new_intent(
    database: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.app.services.content_validation_service import ContentValidationService
    from backend.app.services.file_storage_service import FileStorageService
    from backend.app.services.question_asset_service import QuestionAssetService
    from tests.unit.services.test_content_validation_service import manual
    from tests.unit.services.test_question_asset_service import png
    from tests.unit.settings_helpers import build_test_settings

    engine, info = database
    settings = build_test_settings(storage_root=tmp_path)
    monkeypatch.setattr(
        "backend.app.services.file_storage_service.get_settings", lambda: settings
    )
    with Session(engine) as session:
        identity = info["questions"][2]
        questions = QuestionService(session)
        questions.update_question_status(
            identity,
            "Needs Revision",
            teacher_id=info["actor"],
            revision_comment="Synthetic original-image assembly case.",
        )
        asset = QuestionAssetService(session, root=tmp_path).upload_question(
            identity,
            content=png(),
            asset_type="figure",
            caption="One original black pixel",
            actor_id=info["actor"],
        )
        questions.update_question_status(
            identity, "Pending Review", teacher_id=info["actor"]
        )
        content = ContentValidationService(session, root=tmp_path)
        view = content.get_image_assessment(
            "question", identity, actor_id=info["actor"]
        )
        content.manual_image_check(
            "question",
            identity,
            manual(
                view,
                asset,
                explanation="Synthetic fixture checks actual original bytes; no independent teacher claim.",
            ),
            actor_id=info["actor"],
        )
        persist_current_semantic_pass(session, identity, info["actor"], root=tmp_path)
        questions.update_question_status(identity, "Approved", teacher_id=info["actor"])
        first = ExamAssemblyService(session, root=tmp_path).assemble(
            info["exam"], request(info), actor_id=info["actor"]
        )
        projected = next(
            item for item in first.exam_questions if item.question_id == identity
        )
        assert [item.id for item in projected.assets] == [asset.id]
        path, _ = FileStorageService(session, root=tmp_path).download(
            asset.file_id, actor_id=info["actor"]
        )
        assert path.is_relative_to(tmp_path.resolve())
        path.unlink()
        before = facts(session, info["exam"])
        payload = request(info, total_score="6.00")
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session, root=tmp_path).assemble(
                info["exam"], payload, actor_id=info["actor"]
            )
        assert error.value.code == "EXAM_ASSEMBLY_UNSATISFIED"
        assert error.value.as_detail()["intent_saved"] is True
        assert any(
            item["question_id"] == str(identity)
            for item in error.value.as_detail()["excluded_candidates"]
        )
        assert facts(session, info["exam"]) == before
    with Session(engine) as session:
        assert (
            session.get(Exam, info["exam"]).assembly_constraints["request"][
                "total_score"
            ]
            == "6.00"
        )


def test_intent_flush_failure_is_known_unsaved(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, info = database
    with Session(engine) as session:
        before = facts(session, info["exam"])
        flush = session.flush

        def fail_intent(objects: Any = None) -> None:
            if any(
                isinstance(item, Exam) and item.assembly_constraints is not None
                for item in session.dirty
            ):
                raise SQLAlchemyError("controlled intent flush failure")
            flush(objects)

        monkeypatch.setattr(session, "flush", fail_intent)
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session).assemble(
                info["exam"], request(info), actor_id=info["actor"]
            )
        assert error.value.as_detail()["intent_saved"] is False
        assert error.value.as_detail()["intent_save_error"]
        assert facts(session, info["exam"]) == before
    with Session(engine) as session:
        assert session.get(Exam, info["exam"]).assembly_constraints is None


def test_same_exam_operations_serialize_intent_and_facts(database: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    engine, info = database
    first_holds_lock = Event()
    release_first = Event()
    second_tag = "t172-" + uuid4().hex

    class PausingAssembly(ExamAssemblyService):
        def _replace_questions(self, *args: Any, **kwargs: Any) -> None:
            first_holds_lock.set()
            assert release_first.wait(10), "test must release the first assembly"
            super()._replace_questions(*args, **kwargs)

    def assemble_first() -> None:
        with Session(engine) as session:
            PausingAssembly(session).assemble(
                info["exam"], request(info), actor_id=info["actor"]
            )

    def assemble_second() -> None:
        with Session(engine) as session:
            session.execute(
                text("SELECT set_config('application_name', :name, true)"),
                {"name": second_tag},
            )
            ExamAssemblyService(session).assemble(
                info["exam"], request(info, total_score="6.00"), actor_id=info["actor"]
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(assemble_first)
        try:
            assert first_holds_lock.wait(10)
            second = executor.submit(assemble_second)
            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline:
                with engine.connect() as connection:
                    blocked = (
                        connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name AND wait_event_type = 'Lock'"
                            ),
                            {"name": second_tag},
                        )
                        > 0
                    )
                if blocked:
                    break
                sleep(0.02)
            assert (
                blocked
            ), "the second same-exam command must wait for the first transaction"
        finally:
            release_first.set()
        first.result(timeout=10)
        second.result(timeout=10)
    with Session(engine) as session:
        result = ExamAssemblyService(session).preview(
            info["exam"], actor_id=info["actor"]
        )
        assert result.assembly_constraints.request.total_score == Decimal("6.00")
        assert result.total_score == Decimal("6.00")
        assert {item.question_id for item in result.exam_questions} == {
            info["questions"][1],
            info["questions"][2],
        }


def test_preview_uses_fixed_historical_labels_and_unknown_scores(database: Any) -> None:
    engine, info = database
    with Session(engine) as session:
        exam = session.get(Exam, info["exam"])
        from backend.app.schemas.exam_assembly import AssemblyConstraints

        exam.assembly_constraints = AssemblyConstraints(
            request=request(
                info,
                question_count=1,
                type_distribution=[{"question_type": "SHORT_ANSWER", "count": 1}],
                total_score="3.00",
                knowledge_coverage=[{"knowledge_point": "K2", "min_questions": 1}],
            ),
            recorded_by=info["actor"],
            recorded_at=datetime.now(UTC),
        ).model_dump(mode="json")
        exam.status = ExamStatus.PUBLISHED
        exam.exam_question_links[0].question.knowledge_points = ["Changed metadata"]
        session.commit()
        response = ExamAssemblyService(session).preview(
            info["exam"], actor_id=info["actor"]
        )
        assert all(item.satisfied for item in response.conditions)
        exam.exam_question_links[0].score = None
        exam.exam_question_links[0].published_knowledge_points = None
        session.commit()
        response = ExamAssemblyService(session).preview(
            info["exam"], actor_id=info["actor"]
        )
        assert response.total_score is None
        assert (
            next(
                item for item in response.conditions if item.kind == "knowledge:K2"
            ).actual
            is None
        )


def test_admission_database_failure_is_structured_and_unsaved(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, info = database
    with Session(engine) as session:
        service = ExamAssemblyService(session)

        def failed_load(*args: Any, **kwargs: Any) -> None:
            raise SQLAlchemyError("controlled admission read failure")

        monkeypatch.setattr(service, "_load_exam", failed_load)
        with pytest.raises(AssemblyError) as error:
            service.assemble(info["exam"], request(info), actor_id=info["actor"])
        assert error.value.http_status == 503
        assert error.value.as_detail()["intent_saved"] is False
        assert error.value.as_detail()["error"]["cause"] == "SQLAlchemyError"
        assert session.get(Exam, info["exam"]).assembly_constraints is None


def test_failed_rollback_does_not_replace_unknown_commit_outcome(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, info = database
    with Session(engine) as session:
        commit, rollback = session.commit, session.rollback

        def failed_commit() -> None:
            commit()
            raise SQLAlchemyError("controlled lost commit acknowledgement")

        def failed_cleanup() -> None:
            rollback()
            raise SQLAlchemyError("controlled rollback failure")

        monkeypatch.setattr(session, "commit", failed_commit)
        monkeypatch.setattr(session, "rollback", failed_cleanup)
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session).assemble(
                info["exam"], request(info), actor_id=info["actor"]
            )
        assert error.value.code == "EXAM_ASSEMBLY_COMMIT_UNKNOWN"
        assert error.value.as_detail()["intent_saved"] is None
        assert error.value.as_detail()["intent_save_error"]
        assert error.value.as_detail()["cleanup_error"]
    with Session(engine) as session:
        assert session.get(Exam, info["exam"]).assembly_constraints is not None


def test_draft_with_submission_history_is_not_editable(database: Any) -> None:
    from backend.app.models import Submission

    engine, info = database
    with Session(engine) as session:
        student = add_user(
            session,
            UserRole.STUDENT,
            username="assembly-student",
            email="assembly-student@example.com",
        )
        session.flush()
        session.add(Submission(exam_id=info["exam"], student_id=student.id))
        session.commit()
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session).assemble(
                info["exam"], request(info), actor_id=info["actor"]
            )
        assert error.value.code == "EXAM_PUBLISHED_IMMUTABLE"
        assert facts(session, info["exam"]) == before
        assert session.get(Exam, info["exam"]).assembly_constraints is None


@pytest.mark.parametrize("case", ["exam", "question"])
def test_locks_refresh_stale_identity_map(database: Any, case: str) -> None:
    engine, info = database
    with Session(engine, expire_on_commit=False) as session:
        old_exam = session.get(Exam, info["exam"])
        old_question = session.get(Question, info["questions"][2])
        session.commit()
        with Session(engine) as writer:
            if case == "exam":
                writer.get(Exam, info["exam"]).status = ExamStatus.PUBLISHED
            else:
                writer.get(Question, info["questions"][2]).validation_revision += 1
            writer.commit()
        assert old_exam.status == ExamStatus.DRAFT
        assert old_question.validation_revision == 0
        with pytest.raises(AssemblyError) as error:
            ExamAssemblyService(session).assemble(
                info["exam"], request(info), actor_id=info["actor"]
            )
        assert error.value.code == (
            "EXAM_PUBLISHED_IMMUTABLE"
            if case == "exam"
            else "EXAM_ASSEMBLY_UNSATISFIED"
        )


def test_course_exam_and_candidate_locks_have_one_global_question_order(
    database: Any,
) -> None:
    from sqlalchemy import event

    engine, info = database
    locks: list[str] = []

    def observe(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: Any,
    ) -> None:
        if "FOR UPDATE" in statement:
            locks.append(statement.lower())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with Session(engine) as session:
            ExamAssemblyService(session).assemble(
                info["exam"],
                request(
                    info,
                    score_overrides=[
                        {"question_id": info["questions"][3], "score": "99.00"}
                    ],
                ),
                actor_id=info["actor"],
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert "from courses" in locks[0]
    assert "from exams" in locks[1]
    assert "from questions" in locks[2]
    # The initial ordered lock covers both the candidate scope and overrides,
    # including an override outside requested types, before any per-question reads.
    assert "order by questions.id" in locks[2]
    assert " or questions.id in" in locks[2]
