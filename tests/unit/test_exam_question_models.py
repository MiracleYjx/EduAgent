"""T170 association/schema boundaries, authorized by TCR §25."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from backend.app.domain.enums import ExamStatus, QuestionType
from backend.app.models import Base, Course, Exam, ExamQuestion, Question, User
from backend.app.schemas.exam_assembly import AssemblyConstraints, AssemblyRequest
from backend.app.schemas.exam_scoring import ScoringBasis


def request_data():
    return {
        "course_id": str(uuid4()),
        "question_count": 2,
        "type_distribution": [{"question_type": "SHORT_ANSWER", "count": 2}],
        "knowledge_coverage": [{"knowledge_point": "loops", "min_questions": 1}],
        "total_score": "2000000.00",
        "score_overrides": [],
    }


def test_assembly_request_retains_exact_decimal_and_full_validated_intent():
    value = AssemblyRequest.model_validate(request_data())
    assert value.total_score == Decimal("2000000.00")
    assert value.model_dump(mode="json")["total_score"] == "2000000.00"
    intent = AssemblyConstraints(
        request=value, recorded_by=uuid4(), recorded_at=datetime.now(UTC)
    )
    assert AssemblyConstraints.model_validate(intent.model_dump(mode="json")) == intent


@pytest.mark.parametrize(
    "change",
    [
        {"question_count": True},
        {"question_count": 201},
        {"total_score": 1.5},
        {"total_score": "1.001"},
        {"total_score": "NaN"},
        {"total_score": "Infinity"},
        {"total_score": "0"},
        {"type_distribution": [{"question_type": "SHORT_ANSWER", "count": 1}]},
        {"type_distribution": [{"question_type": "SHORT_ANSWER", "count": 1}] * 2},
        {"knowledge_coverage": [{"knowledge_point": "  ", "min_questions": 1}]},
        {"knowledge_coverage": [{"knowledge_point": "loops", "min_questions": 1}] * 2},
        {"unknown": 1},
    ],
)
def test_assembly_request_rejects_ambiguous_inputs(change):
    with pytest.raises(ValidationError):
        AssemblyRequest.model_validate(request_data() | change)


@pytest.mark.parametrize("score", ["-1", "0", "1000000.00", "1.001", 1, True])
def test_override_preserves_single_question_amount_limit(score):
    with pytest.raises(ValidationError):
        AssemblyRequest.model_validate(
            request_data()
            | {"score_overrides": [{"question_id": str(uuid4()), "score": score}]}
        )


def test_scoring_basis_preserves_negative_delta_without_fabricated_confirmation():
    basis = ScoringBasis.model_validate(
        {
            "kind": "subjective",
            "rounding_mode": "ROUND_HALF_UP",
            "additive": True,
            "points": [
                {
                    "key": str(i),
                    "label": "point",
                    "base_points": "1.00",
                    "default_points": "0.67",
                    "confirmed_points": "0.67",
                }
                for i in range(3)
            ],
            "rounding_delta": "-0.01",
            "confirmation": None,
        }
    )
    assert basis.rounding_delta == Decimal("-0.01")
    assert basis.model_dump(mode="json")["rounding_delta"] == "-0.01"
    assert basis.confirmation is None
    with pytest.raises(ValidationError):
        ScoringBasis.model_validate(
            basis.model_dump(mode="json")
            | {"points": [basis.points[0].model_dump(mode="json")] * 2}
        )


def test_association_is_single_write_source_and_legacy_relationships_read_same_order():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            teacher = User(
                username="teacher",
                email="teacher@example.test",
                password_hash="fixture",
            )
            course = Course(name="course", creator=teacher)
            questions = [
                Question(
                    course=course,
                    creator=teacher,
                    type=QuestionType.SHORT_ANSWER,
                    content=str(i),
                    score=Decimal("5.00"),
                )
                for i in range(2)
            ]
            exam = Exam(course=course, creator=teacher, title="exam")
            exam.exam_question_links = [
                ExamQuestion(
                    question=questions[1], order_index=1, score=Decimal("3.00")
                ),
                ExamQuestion(question=questions[0], order_index=2),
            ]
            session.add(exam)
            session.commit()
            session.expire_all()
            actual = session.scalar(
                select(Exam).options(
                    selectinload(Exam.questions), selectinload(Exam.exam_question_links)
                )
            )
            assert [q.content for q in actual.questions] == ["1", "0"]
            assert actual.questions[0].exams == [actual]
            assert [link.effective_score for link in actual.exam_question_links] == [
                Decimal("3.00"),
                Decimal("5.00"),
            ]
            assert actual.assembly_constraints is None
            assert all(
                link.base_score is None and link.scoring_basis is None
                for link in actual.exam_question_links
            )
            actual.status = ExamStatus.PUBLISHED
            with pytest.raises(ValueError, match="EXAM_SCORING_BASIS_MISSING"):
                _ = actual.exam_question_links[1].effective_score
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("order_index", 0),
        ("score", Decimal("0.00")),
        ("base_score", Decimal("-0.01")),
        ("score", Decimal("1000000.00")),
    ],
)
def test_database_rejects_invalid_order_or_amount(field, value):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            teacher = User(
                username="teacher",
                email="teacher@example.test",
                password_hash="fixture",
            )
            course = Course(name="course", creator=teacher)
            question = Question(
                course=course,
                creator=teacher,
                type=QuestionType.SHORT_ANSWER,
                content="question",
                score=Decimal("5.00"),
            )
            exam = Exam(course=course, creator=teacher, title="exam")
            link = ExamQuestion(question=question, order_index=1)
            setattr(link, field, value)
            exam.exam_question_links.append(link)
            session.add(exam)
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()
