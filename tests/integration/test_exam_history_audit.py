"""T171: verified historical evidence, no inferred facts or modified old results.

TCR: docs/test-change-record-v2.md §26. Real PostgreSQL transactions establish
read-only inventory, complete evidence, identity, idempotence and rollback.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    UserRole,
    ValidationStatus,
)
from backend.app.models import Answer, Course, Exam, Question, Role, Submission, User
from backend.app.models.exam_question import ExamQuestion
from backend.app.models.exam_result import ExamResult
from backend.app.models.grading_result import GradingResult
from backend.app.models.review_record import ReviewRecord
from backend.app.schemas.grading import ExamResultStatus
from backend.app.services.exam_history_service import (
    ExamHistoryError,
    ExamHistoryManifest,
    ExamHistoryService,
)
from tests.postgres_helpers import isolated_postgres_engine

OLD = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)


@pytest.fixture
def history(tmp_path: Path):
    with isolated_postgres_engine() as engine:
        with Session(engine) as session:
            teacher = User(
                username="history-owner",
                email="history-owner@example.test",
                password_hash="not-a-login",
            )
            teacher.roles = [Role(name=UserRole.ADMIN), Role(name=UserRole.TEACHER)]
            other = User(
                username="history-admin",
                email="history-admin@example.test",
                password_hash="not-a-login",
            )
            other.roles = list(teacher.roles)
            student = User(
                username="history-student",
                email="history-student@example.test",
                password_hash="not-a-login",
            )
            course = Course(name="T171 synthetic historical course", creator=teacher)
            questions = [
                Question(
                    course=course,
                    creator=teacher,
                    type=QuestionType.SINGLE_CHOICE,
                    content=f"Synthetic question {i}",
                    options={"A": "yes", "B": "no"},
                    reference_answer="A",
                    scoring_rubric="Current text cannot prove old rubric.",
                    score=Decimal("99.00"),
                    knowledge_points=["current-only"],
                    status=QuestionStatus.APPROVED,
                    frozen_at=None,
                    created_at=OLD,
                    updated_at=OLD,
                )
                for i in range(2)
            ]
            exam = Exam(
                course=course,
                creator=teacher,
                title="Synthetic historical exam",
                status=ExamStatus.PUBLISHED,
                created_at=OLD,
                updated_at=OLD,
            )
            session.add_all([exam, other, student, *questions])
            session.flush()
            links = [
                ExamQuestion(
                    exam_id=exam.id, question_id=q.id, order_index=i, created_at=OLD
                )
                for i, q in enumerate(questions, 1)
            ]
            session.add_all(links)
            submission = Submission(
                exam=exam,
                student=student,
                status=SubmissionStatus.REVIEWED,
                submitted_at=OLD,
                graded_at=OLD,
                reviewed_at=OLD,
                created_at=OLD,
                updated_at=OLD,
            )
            answers = [
                Answer(
                    submission=submission,
                    question=q,
                    content="PRIVATE-STUDENT-ANSWER",
                    status=AnswerStatus.GRADED,
                    created_at=OLD,
                    updated_at=OLD,
                )
                for q in questions
            ]
            session.add_all(answers)
            session.flush()
            grade = GradingResult(
                answer_id=answers[0].id,
                submission_id=submission.id,
                question_type=QuestionType.SINGLE_CHOICE,
                score=Decimal("4.00"),
                max_score=Decimal("5.00"),
                reason="Original grading fact",
                confidence=1,
                knowledge_points=["runtime-only"],
                review_status=ReviewStatus.CONFIRMED,
                validation_status=ValidationStatus.VALIDATED,
                created_at=OLD,
                updated_at=OLD,
            )
            session.add(grade)
            session.flush()
            session.add(
                ExamResult(
                    submission_id=submission.id,
                    exam_id=exam.id,
                    student_id=student.id,
                    result_status=ExamResultStatus.FINAL,
                    is_final=True,
                    final_total_score=Decimal("4.00"),
                    confirmed_subtotal=Decimal("4.00"),
                    total_max_score=Decimal("10.00"),
                    aggregated_at=OLD,
                    created_at=OLD,
                    updated_at=OLD,
                )
            )
            session.add(
                ReviewRecord(
                    grading_result_id=grade.id,
                    reviewer_id=teacher.id,
                    decision=ReviewStatus.CONFIRMED,
                    original_score=Decimal("4.00"),
                    final_score=Decimal("4.00"),
                    original_reason="Original grading fact",
                    final_reason="Original grading fact",
                    original_knowledge_points=["runtime-only"],
                    final_knowledge_points=["runtime-only"],
                    created_at=OLD,
                    updated_at=OLD,
                )
            )
            session.commit()
            ids = {
                "actor": teacher.id,
                "other": other.id,
                "exam": exam.id,
                "questions": [q.id for q in questions],
                "links": [row.id for row in links],
                "grade": grade.id,
            }
        evidence = tmp_path / "synthetic-original-record.json"
        evidence.write_text(
            json.dumps(
                {
                    "source_kind": "synthetic-test-fixture",
                    "exam_id": str(ids["exam"]),
                    "questions": [
                        {
                            "question_id": str(qid),
                            "order_index": i,
                            "score": "5.00",
                            "base_score": "5.00",
                            "knowledge_points": ["publication-record"],
                            "original_standard": "客观题答案A正确给本场5分，无数值要点或尾差。",
                        }
                        for i, qid in enumerate(ids["questions"], 1)
                    ],
                }
            ),
            encoding="utf-8",
        )
        manifest = {
            "format_version": 1,
            "evidence_files": [
                {
                    "id": "original",
                    "path": str(evidence),
                    "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                }
            ],
            "exams": [
                {
                    "exam_id": str(ids["exam"]),
                    "reason": "Synthetic fixture: explicitly checked original publication evidence; current check, not old approval.",
                    "questions": [
                        {
                            "question_id": str(qid),
                            "order_index": i,
                            "score": "5.00",
                            "base_score": "5.00",
                            "published_knowledge_points": ["publication-record"],
                            "scoring_basis": {
                                "kind": "objective",
                                "points": [],
                                "additive": False,
                                "rounding_delta": None,
                                "confirmation": None,
                            },
                            "evidence_refs": {
                                key: ["original"]
                                for key in (
                                    "order_index",
                                    "score",
                                    "base_score",
                                    "published_knowledge_points",
                                    "scoring_basis",
                                )
                            },
                        }
                        for i, qid in enumerate(ids["questions"], 1)
                    ],
                }
            ],
        }
        yield engine, ids, manifest, evidence


def immutable_rows(session: Session):
    return {
        model.__tablename__: [
            dict(row) for row in session.execute(select(model.__table__)).mappings()
        ]
        for model in (
            Question,
            Exam,
            Submission,
            Answer,
            GradingResult,
            ExamResult,
            ReviewRecord,
        )
    }


def test_audit_is_read_only_and_runtime_evidence_is_not_publication_fact(history):
    engine, ids, _, _ = history
    with Session(engine) as session:
        before = immutable_rows(session)
        report = ExamHistoryService(session).audit(actor_id=ids["actor"])
        assert immutable_rows(session) == before
        value = report.model_dump(mode="json")
        assert value["exams"][0]["status"] == "history_unknown"
        row = value["exams"][0]["questions"][0]
        assert set(row["missing_fields"]) == {
            "score",
            "base_score",
            "published_knowledge_points",
            "scoring_basis",
        }
        observed = row["grading_evidence"][0]
        assert observed["max_score"] == "5.00"
        assert observed["knowledge_points"] == ["runtime-only"]
        assert "PRIVATE-STUDENT-ANSWER" not in report.model_dump_json()
        assert all(link.score is None for link in session.scalars(select(ExamQuestion)))


def test_apply_preserves_results_and_unknown_approval_time_and_is_idempotent(history):
    engine, ids, raw, _ = history
    manifest = ExamHistoryManifest.model_validate(raw)
    with Session(engine) as session:
        before = immutable_rows(session)
        first = ExamHistoryService(session).apply(
            actor_id=ids["actor"], manifest=manifest
        )
        assert first.exams[0].status == "applied"
        rows = list(
            session.scalars(select(ExamQuestion).order_by(ExamQuestion.order_index))
        )
        stored = [dict(row.scoring_basis) for row in rows]
        assert all(row.score == Decimal("5.00") for row in rows)
        assert all(row.base_score == Decimal("5.00") for row in rows)
        assert all(
            row.published_knowledge_points == ["publication-record"] for row in rows
        )
        assert all(
            row.scoring_basis["confirmation"]["teacher_id"] == str(ids["actor"])
            for row in rows
        )
        assert all(
            datetime.fromisoformat(row.scoring_basis["confirmation"]["confirmed_at"])
            > OLD
            for row in rows
        )
        assert immutable_rows(session) == before
        second = ExamHistoryService(session).apply(
            actor_id=ids["actor"], manifest=manifest
        )
        assert second.exams[0].status == "unchanged"
        assert [row.scoring_basis for row in rows] == stored
        assert immutable_rows(session) == before


@pytest.mark.parametrize(
    "change", ["partial", "foreign", "forged_confirmation", "missing_ref", "bad_basis"]
)
def test_incomplete_or_unverified_manifest_never_changes_rows(history, change):
    engine, ids, raw, _ = history
    if change == "partial":
        raw["exams"][0]["questions"].pop()
    if change == "foreign":
        raw["exams"][0]["questions"][0][
            "question_id"
        ] = "00000000-0000-0000-0000-000000000001"
    if change == "forged_confirmation":
        raw["exams"][0]["questions"][0]["scoring_basis"]["confirmation"] = {
            "teacher_id": str(ids["other"]),
            "confirmed_at": OLD.isoformat(),
            "reason": "forged",
        }
    if change == "missing_ref":
        raw["exams"][0]["questions"][0]["evidence_refs"]["score"] = []
    if change == "bad_basis":
        raw["exams"][0]["questions"][0]["scoring_basis"] = {
            "kind": "objective",
            "points": [],
            "additive": True,
            "rounding_delta": "1.00",
            "confirmation": None,
        }
    with Session(engine) as session:
        with pytest.raises((ExamHistoryError, ValidationError)):
            ExamHistoryService(session).apply(
                actor_id=ids["actor"], manifest=ExamHistoryManifest.model_validate(raw)
            )
        session.rollback()
        assert all(link.score is None for link in session.scalars(select(ExamQuestion)))


def test_admin_cannot_claim_another_courses_teacher_confirmation(history):
    engine, ids, raw, _ = history
    with Session(engine) as session:
        assert ExamHistoryService(session).audit(actor_id=ids["other"]).exams
        with pytest.raises(ExamHistoryError) as error:
            ExamHistoryService(session).apply(
                actor_id=ids["other"], manifest=ExamHistoryManifest.model_validate(raw)
            )
        assert error.value.code == "EXAM_HISTORY_FORBIDDEN"
        assert all(link.score is None for link in session.scalars(select(ExamQuestion)))


def test_changed_evidence_bytes_are_rejected_before_write(history):
    engine, ids, raw, evidence = history
    evidence.write_text("changed after review", encoding="utf-8")
    with Session(engine) as session:
        with pytest.raises(ExamHistoryError) as error:
            ExamHistoryService(session).apply(
                actor_id=ids["actor"], manifest=ExamHistoryManifest.model_validate(raw)
            )
        assert error.value.code == "EXAM_HISTORY_EVIDENCE_MISMATCH"
        assert all(link.score is None for link in session.scalars(select(ExamQuestion)))


def test_conflicting_fixed_fact_keeps_whole_exam_unchanged(history):
    engine, ids, raw, _ = history
    with Session(engine) as session:
        row = session.get(ExamQuestion, ids["links"][1])
        row.score = Decimal("6.00")
        session.commit()
        with pytest.raises(ExamHistoryError) as error:
            ExamHistoryService(session).apply(
                actor_id=ids["actor"], manifest=ExamHistoryManifest.model_validate(raw)
            )
        assert error.value.code == "EXAM_HISTORY_CONFLICT"
        assert session.get(ExamQuestion, ids["links"][0]).score is None
        assert session.get(ExamQuestion, ids["links"][1]).score == Decimal("6.00")


def test_commit_failure_rolls_back_all_rows_and_explicit_retry_succeeds(history):
    engine, ids, raw, _ = history
    with Session(engine) as session:

        @event.listens_for(session, "before_commit", once=True)
        def fail_commit(_session):
            raise RuntimeError("injected commit interruption")

        with pytest.raises(RuntimeError, match="injected commit interruption"):
            ExamHistoryService(session).apply(
                actor_id=ids["actor"], manifest=ExamHistoryManifest.model_validate(raw)
            )
        assert all(link.score is None for link in session.scalars(select(ExamQuestion)))
        result = ExamHistoryService(session).apply(
            actor_id=ids["actor"], manifest=ExamHistoryManifest.model_validate(raw)
        )
        assert result.exams[0].status == "applied"


def test_stronger_order_evidence_preserves_association_identity(history):
    engine, ids, raw, evidence = history
    raw["exams"][0]["questions"][0]["order_index"] = 2
    raw["exams"][0]["questions"][1]["order_index"] = 1
    original = json.loads(evidence.read_text(encoding="utf-8"))
    original["questions"][0]["order_index"] = 2
    original["questions"][1]["order_index"] = 1
    evidence.write_text(json.dumps(original), encoding="utf-8")
    raw["evidence_files"][0]["sha256"] = hashlib.sha256(
        evidence.read_bytes()
    ).hexdigest()
    with Session(engine) as session:
        before = immutable_rows(session)
        ExamHistoryService(session).apply(
            actor_id=ids["actor"], manifest=ExamHistoryManifest.model_validate(raw)
        )
        ordered = list(
            session.scalars(select(ExamQuestion).order_by(ExamQuestion.order_index))
        )
        assert [row.question_id for row in ordered] == list(reversed(ids["questions"]))
        assert {row.id for row in ordered} == set(ids["links"])
        assert immutable_rows(session) == before


def test_cli_reports_committed_facts_when_final_report_write_fails(
    history, monkeypatch, tmp_path, capsys
):
    from scripts import audit_exam_history as cli

    engine, ids, raw, _ = history
    manifest_path = tmp_path / "reviewed.json"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    report_path = tmp_path / "report.json"
    monkeypatch.setenv("T171_TEST_TOKEN", "fixture-token-used-only-by-auth-double")
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_exam_history.py",
            "--token-env",
            "T171_TEST_TOKEN",
            "--apply",
            str(manifest_path),
            "--report",
            str(report_path),
        ],
    )
    monkeypatch.setattr(cli, "get_session_factory", lambda: lambda: Session(engine))
    monkeypatch.setattr(
        cli,
        "authenticate_admin",
        lambda session, token: session.get(User, ids["actor"]),
    )
    original = cli.durable_json
    calls = []

    def fail_final_once(path, value, *, create=False):
        calls.append(create)
        if len(calls) == 2:
            raise OSError("injected final report storage failure")
        original(path, value, create=create)

    monkeypatch.setattr(cli, "durable_json", fail_final_once)
    assert cli.main() == 1
    recorded = json.loads(report_path.read_text(encoding="utf-8"))
    assert recorded["status"] == "report_failed_after_commit"
    assert recorded["database_committed"] is True
    assert "fixture-token" not in capsys.readouterr().err
    with Session(engine) as session:
        assert all(
            row.score == Decimal("5.00")
            for row in session.scalars(select(ExamQuestion))
        )


def test_stale_session_cannot_overwrite_another_sessions_confirmed_history(history):
    engine, ids, raw, _ = history
    with Session(engine) as stale:
        held_exam = stale.get(Exam, ids["exam"])
        held_links = list(
            stale.scalars(
                select(ExamQuestion).where(ExamQuestion.exam_id == ids["exam"])
            )
        )
        assert held_exam is not None
        assert all(row.score is None for row in held_links)
        ExamHistoryService(stale).audit(actor_id=ids["actor"])
        with Session(engine) as writer:
            ExamHistoryService(writer).apply(
                actor_id=ids["actor"], manifest=ExamHistoryManifest.model_validate(raw)
            )
            first_confirmations = {
                row.id: row.scoring_basis["confirmation"]
                for row in writer.scalars(select(ExamQuestion))
            }
        # This later operator supplies conflicting facts while retaining the pre-audit
        # identity map. Row locks must refresh facts before the conflict comparison.
        raw["exams"][0]["questions"][0]["score"] = "6.00"
        with pytest.raises(ExamHistoryError) as error:
            ExamHistoryService(stale).apply(
                actor_id=ids["actor"], manifest=ExamHistoryManifest.model_validate(raw)
            )
        assert error.value.code == "EXAM_HISTORY_CONFLICT"
    with Session(engine) as observer:
        persisted = list(observer.scalars(select(ExamQuestion)))
        assert all(row.score == Decimal("5.00") for row in persisted)
        assert {
            row.id: row.scoring_basis["confirmation"] for row in persisted
        } == first_confirmations


def test_cli_reports_unknown_when_commit_succeeded_but_receipt_was_lost(
    history, monkeypatch, tmp_path
):
    from sqlalchemy.exc import SQLAlchemyError

    from scripts import audit_exam_history as cli

    engine, ids, raw, _ = history
    manifest_path = tmp_path / "reviewed-ambiguous.json"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    report_path = tmp_path / "ambiguous-report.json"
    monkeypatch.setenv("T171_TEST_TOKEN", "fixture-token-used-only-by-auth-double")
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_exam_history.py",
            "--token-env",
            "T171_TEST_TOKEN",
            "--apply",
            str(manifest_path),
            "--report",
            str(report_path),
        ],
    )
    monkeypatch.setattr(cli, "get_session_factory", lambda: lambda: Session(engine))
    monkeypatch.setattr(
        cli,
        "authenticate_admin",
        lambda session, token: session.get(User, ids["actor"]),
    )
    actual_commit = Session.commit

    def commit_then_lose_receipt(session):
        actual_commit(session)
        raise SQLAlchemyError("injected lost COMMIT acknowledgement")

    monkeypatch.setattr(Session, "commit", commit_then_lose_receipt)
    assert cli.main() == 1
    recorded = json.loads(report_path.read_text(encoding="utf-8"))
    assert recorded["status"] == "commit_outcome_unknown"
    assert recorded["database_committed"] is None
    assert recorded["error"]["code"] == "EXAM_HISTORY_COMMIT_UNKNOWN"
    with Session(engine) as observer:
        assert all(
            row.score == Decimal("5.00")
            for row in observer.scalars(select(ExamQuestion))
        )
