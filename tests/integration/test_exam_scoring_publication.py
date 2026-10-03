"""T174 real PostgreSQL publication gates and fixed scoring facts (TCR §29)."""

from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.app.domain.enums import ExamStatus
from backend.app.models import Exam, Question
from backend.app.services.exam_assembly_service import (
    AssemblyError,
    ExamAssemblyService,
)
from backend.app.services.exam_service import ExamService
from tests.integration.test_exam_assembly import database as _assembly_database
from tests.integration.test_exam_assembly import facts, request

database = _assembly_database


def clear_preparation(session, exam_id):
    exam = session.get(Exam, exam_id)
    for link in exam.exam_question_links:
        link.score = None
        link.base_score = None
        link.published_knowledge_points = None
        link.scoring_basis = None
    session.commit()


def test_missing_basis_rejects_publication_without_writing_draft(database):
    engine, info = database
    with Session(engine) as session:
        clear_preparation(session, info["exam"])
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            ExamService(session).publish_exam(info["exam"], teacher_id=info["actor"])
        assert error.value.code == "EXAM_SCORING_BASIS_MISSING"
        assert session.get(Exam, info["exam"]).status == ExamStatus.DRAFT
        assert facts(session, info["exam"]) == before
    with Session(engine) as session:
        assert session.get(Exam, info["exam"]).status == ExamStatus.DRAFT
        assert facts(session, info["exam"]) == before


def test_repeat_published_never_fills_unknown_historical_basis(database):
    engine, info = database
    with Session(engine) as session:
        clear_preparation(session, info["exam"])
        exam = session.get(Exam, info["exam"])
        exam.status = ExamStatus.PUBLISHED
        session.get(Question, info["questions"][1]).knowledge_points = [
            "Changed after historical publication"
        ]
        session.commit()
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            ExamService(session).publish_exam(info["exam"], teacher_id=info["actor"])
        assert error.value.code == "EXAM_SCORING_BASIS_MISSING"
        assert facts(session, info["exam"]) == before
        assert session.get(Exam, info["exam"]).status == ExamStatus.PUBLISHED


def prepared_case(session, info, *, score, additive, points):
    from backend.app.schemas.exam_assembly import ExamQuestionPatchRequest
    from backend.app.services.exam_scoring_service import ExamScoringService
    from tests.integration.test_exam_scoring import prepare_payload

    ExamService(session).patch_exam_question(
        info["exam"],
        info["questions"][1],
        ExamQuestionPatchRequest(score=score),
        teacher_id=info["actor"],
    )
    service = ExamScoringService(session)
    current = service.get_scoring_basis(
        info["exam"], info["questions"][1], teacher_id=info["actor"]
    )
    prepared = service.prepare_scoring_basis(
        info["exam"],
        info["questions"][1],
        prepare_payload(current, additive=additive, points=points),
        teacher_id=info["actor"],
    )
    return service, prepared


@pytest.mark.parametrize(
    "score,additive,points,code",
    [
        ("10.00", True, None, "RUBRIC_ROUNDING_UNCONFIRMED"),
        ("1.01", True, None, "RUBRIC_ROUNDING_UNCONFIRMED"),
        (None, False, [], "RUBRIC_REVIEW_REQUIRED"),
    ],
)
def test_unconfirmed_basis_blocks_publish_without_mutation(
    database, score, additive, points, code
):
    engine, info = database
    with Session(engine) as session:
        clear_preparation(session, info["exam"])
        _, prepared = prepared_case(
            session, info, score=score, additive=additive, points=points
        )
        assert prepared.basis.confirmation is None
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            ExamService(session).publish_exam(info["exam"], teacher_id=info["actor"])
        assert error.value.code == code
        assert session.get(Exam, info["exam"]).status == ExamStatus.DRAFT
        assert facts(session, info["exam"]) == before


def test_confirmed_rounding_publishes_exact_fixed_facts_and_leaves_bank_unchanged(
    database,
):
    from tests.integration.test_exam_scoring import confirm_payload

    engine, info = database
    with Session(engine) as session:
        clear_preparation(session, info["exam"])
        bank = session.get(Question, info["questions"][1])
        original = (bank.score, bank.scoring_rubric, list(bank.knowledge_points))
        scoring, prepared = prepared_case(
            session, info, score="10.00", additive=True, points=None
        )
        confirmed = scoring.confirm_scoring_basis(
            info["exam"],
            info["questions"][1],
            confirm_payload(
                prepared,
                ["3.34", "3.33", "3.33"],
                reason="Synthetic publication check: explicitly assign the positive cent.",
            ),
            teacher_id=info["actor"],
        )
        result = ExamService(session).publish_exam(
            info["exam"], teacher_id=info["actor"]
        )
        assert result.status == ExamStatus.PUBLISHED
        saved = facts(session, info["exam"])[0]
        assert saved[3:6] == (Decimal("10.00"), Decimal("3.00"), original[2])
        assert saved[6] == confirmed.basis.model_dump(mode="json")
        assert saved[6]["confirmation"]["teacher_id"] == str(info["actor"])
        assert [item["default_points"] for item in saved[6]["points"]] == ["3.33"] * 3
        assert [item["confirmed_points"] for item in saved[6]["points"]] == [
            "3.34",
            "3.33",
            "3.33",
        ]
        assert saved[6]["rounding_delta"] == "0.01"
        bank = session.get(Question, info["questions"][1])
        assert (
            bank.score,
            bank.scoring_rubric,
            list(bank.knowledge_points),
        ) == original
    with Session(engine) as session:
        assert facts(session, info["exam"])[0] == saved
        assert session.get(Exam, info["exam"]).status == ExamStatus.PUBLISHED


def test_default_score_fixed_on_publish_and_repeat_does_not_refresh_metadata(database):
    from backend.app.services.question_service import QuestionService
    from tests.unit.services.test_exam_service import prepare_synthetic_exam_scoring

    engine, info = database
    with Session(engine) as session:
        clear_preparation(session, info["exam"])
        prepare_synthetic_exam_scoring(session, info["exam"], info["actor"])
        assert facts(session, info["exam"])[0][3] is None
        service = ExamService(session)
        service.publish_exam(info["exam"], teacher_id=info["actor"])
        before = facts(session, info["exam"])
        assert before[0][3:5] == (Decimal("3.00"), Decimal("3.00"))
        QuestionService(session).update_question(
            info["questions"][1],
            knowledge_points=["Maintained bank metadata"],
            teacher_id=info["actor"],
        )
        again = service.publish_exam(info["exam"], teacher_id=info["actor"])
        assert again.status == ExamStatus.PUBLISHED
        assert facts(session, info["exam"]) == before


def test_same_question_two_exams_keep_independent_fixed_scores_and_confirmations(
    database,
):
    from backend.app.schemas.exam_assembly import ExamQuestionPatchRequest
    from tests.unit.services.test_exam_service import prepare_synthetic_exam_scoring

    engine, info = database
    with Session(engine) as session:
        clear_preparation(session, info["exam"])
        service = ExamService(session)
        other = service.create_exam(
            info["course"],
            "Second independent exam",
            question_ids=[info["questions"][1]],
            teacher_id=info["actor"],
        )
        from uuid import UUID

        other_id = UUID(other.id)
        for exam_id, amount in [(info["exam"], "5.00"), (other_id, "7.00")]:
            service.patch_exam_question(
                exam_id,
                info["questions"][1],
                ExamQuestionPatchRequest(score=amount),
                teacher_id=info["actor"],
            )
            prepare_synthetic_exam_scoring(session, exam_id, info["actor"])
            service.publish_exam(exam_id, teacher_id=info["actor"])
        first, second = facts(session, info["exam"])[0], facts(session, other_id)[0]
        assert first[0] != second[0]
        assert first[1] == second[1] == info["questions"][1]
        assert first[3] == Decimal("5.00") and second[3] == Decimal("7.00")
        assert first[4] == second[4] == Decimal("3.00")
        assert first[6]["preparation_id"] != second[6]["preparation_id"]
        assert session.get(Question, info["questions"][1]).score == Decimal("3.00")


def test_ready_scoring_does_not_bypass_saved_assembly_conditions(database):
    from backend.app.schemas.exam_assembly import ExamQuestionPatchRequest
    from tests.unit.services.test_exam_service import prepare_synthetic_exam_scoring

    engine, info = database
    with Session(engine) as session:
        initial = ExamAssemblyService(session).assemble(
            info["exam"], request(info), actor_id=info["actor"]
        )
        ExamService(session).patch_exam_question(
            info["exam"],
            info["questions"][0],
            ExamQuestionPatchRequest(score="4.00"),
            teacher_id=info["actor"],
        )
        prepare_synthetic_exam_scoring(session, info["exam"], info["actor"])
        before = facts(session, info["exam"])
        intent = deepcopy(session.get(Exam, info["exam"]).assembly_constraints)
        with pytest.raises(AssemblyError) as error:
            ExamService(session).publish_exam(info["exam"], teacher_id=info["actor"])
        assert error.value.code == "EXAM_ASSEMBLY_UNSATISFIED"
        assert session.get(Exam, info["exam"]).status == ExamStatus.DRAFT
        assert facts(session, info["exam"]) == before
        assert (
            session.get(Exam, info["exam"]).assembly_constraints
            == intent
            == initial.assembly_constraints.model_dump(mode="json")
        )


@pytest.mark.parametrize("operation", ["preview", "move"])
def test_question_lock_wait_refreshes_invalidated_scoring_basis(
    database, monkeypatch, operation
):
    from sqlalchemy import select

    from backend.app.schemas.exam_assembly import ExamQuestionPatchRequest
    from backend.app.services.question_scoring_invalidation import (
        bump_question_validation,
    )
    from tests.unit.services.test_exam_service import prepare_synthetic_exam_scoring

    engine, info = database
    with Session(engine) as session:
        prepare_synthetic_exam_scoring(session, info["exam"], info["actor"])
        session.commit()
        scalars = session.scalars
        invalidated = False

        def invalidate_before_question_lock(statement, *args, **kwargs):
            nonlocal invalidated
            entities = getattr(statement, "column_descriptions", [])
            if (
                not invalidated
                and getattr(statement, "_for_update_arg", None) is not None
                and any(item.get("entity") is Question for item in entities)
            ):
                invalidated = True
                # The main command already holds Course/Exam and cached the link.
                # An earlier Question writer completes its real invalidation before
                # the command obtains that Question lock; no alternate database is used.
                with Session(engine) as writer:
                    question = writer.scalars(
                        select(Question)
                        .where(Question.id == info["questions"][1])
                        .with_for_update()
                    ).one()
                    bump_question_validation(writer, question)
                    writer.commit()
            return scalars(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", invalidate_before_question_lock)
        if operation == "preview":
            response = ExamAssemblyService(session).preview(
                info["exam"], actor_id=info["actor"]
            )
        else:
            response = ExamService(session).patch_exam_question(
                info["exam"],
                info["questions"][1],
                ExamQuestionPatchRequest(order_index=1),
                teacher_id=info["actor"],
            )
        assert invalidated
        assert response.exam_questions[0].scoring_basis is None
        assert response.exam_questions[0].base_score is None
        assert "EXAM_SCORING_BASIS_MISSING" in {
            check.code for check in response.publication_checks
        }
    with Session(engine) as session:
        link = session.get(Exam, info["exam"]).exam_question_links[0]
        assert link.scoring_basis is None and link.base_score is None
