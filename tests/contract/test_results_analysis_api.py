"""T180: real isolated stored outcomes, actual denominators and authorization.

TCR: docs/test-change-record-v2.md §35. Synthetic developer-reviewed inputs;
no teacher identity, cloud quality, UI or SC-013 acceptance is claimed.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.api.results import ResultsQueryService
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
    WorkflowStatus,
)
from backend.app.models import (
    Answer,
    Course,
    Exam,
    ExamParticipant,
    ExamQuestion,
    Question,
    Submission,
    WorkflowRun,
)
from backend.app.schemas.grading import (
    ExamResultDTO,
    ExamResultStatus,
    QuestionResultDTO,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from tests.contract import test_results_api_contract as result_contracts
from tests.contract.test_results_api_contract import headers
from tests.unit.services.test_submission_service import (
    add_student,
    add_teacher,
    add_user,
)

session = result_contracts.session
client_factory = result_contracts.client_factory


@pytest.fixture
def analysis_scenario(session):
    teacher = add_teacher(session)
    course = Course(name="考情夹具", created_by=teacher.id)
    session.add(course)
    session.flush()
    questions = [
        Question(
            course_id=course.id,
            created_by=teacher.id,
            type=QuestionType.SINGLE_CHOICE,
            content=f"原题{i}",
            reference_answer="A",
            score=Decimal("99.00"),
            status=QuestionStatus.APPROVED,
            knowledge_points=["当前题库标签"],
        )
        for i in range(2)
    ]
    session.add_all(questions)
    session.flush()
    exam = Exam(
        course_id=course.id,
        created_by=teacher.id,
        title="固定分值考试",
        status=ExamStatus.PUBLISHED,
    )
    exam.exam_question_links = [
        ExamQuestion(
            question=q,
            order_index=i + 1,
            score=Decimal(score),
            base_score=Decimal("99.00"),
            published_knowledge_points=tags,
        )
        for i, (q, score, tags) in enumerate(
            zip(questions, ["2.00", "3.00"], [["一次函数"], ["一次函数", "表达与计算"]])
        )
    ]
    session.add(exam)
    session.commit()
    students = [add_student(session, username=f"s{i}") for i in range(7)]
    session.add_all(
        ExamParticipant(exam_id=exam.id, student_id=student.id) for student in students
    )
    submissions = []
    for i, student in enumerate(students[:6]):
        sub = Submission(
            exam_id=exam.id,
            student_id=student.id,
            status=SubmissionStatus.DRAFT if i == 5 else SubmissionStatus.SUBMITTED,
            submitted_at=None if i == 5 else datetime.now(UTC),
        )
        session.add(sub)
        session.flush()
        session.add_all(
            Answer(
                submission_id=sub.id,
                question_id=q.id,
                content="B",
                status=AnswerStatus.SUBMITTED,
            )
            for q in questions
        )
        submissions.append(sub)
    session.commit()
    repository = DatabaseGradingRepository(
        session_factory=lambda: type(session)(session.get_bind())
    )
    service = ResultsQueryService(session=session, repository=repository)
    return {
        "teacher": teacher,
        "course": course,
        "questions": questions,
        "exam": exam,
        "students": students,
        "submissions": submissions,
        "repository": repository,
        "service": service,
    }


def save_result(session, scenario, index, scores, *, pending=False):
    sub = scenario["submissions"][index]
    answers = {
        a.question_id: a
        for a in session.scalars(select(Answer).where(Answer.submission_id == sub.id))
    }
    items = []
    for link, score in zip(scenario["exam"].exam_question_links, scores):
        items.append(
            QuestionResultDTO(
                order=link.order_index,
                answer_id=str(answers[link.question_id].id),
                question_id=str(link.question_id),
                exam_question_id=str(link.id),
                submission_id=str(sub.id),
                question_type=QuestionType.SINGLE_CHOICE,
                max_score=link.score,
                score=Decimal(score),
                effective_score=None if pending else Decimal(score),
                counted=not pending,
                requires_review=pending,
                grading_status="Pending Review" if pending else "Accepted",
                review_status="Pending Review" if pending else "Not Required",
                validation_status="Validated",
                knowledge_points=link.published_knowledge_points,
                reason="实际评分理由",
            )
        )
    total = sum((Decimal(s) for s in scores), Decimal(0))
    result = ExamResultDTO(
        submission_id=str(sub.id),
        exam_id=str(sub.exam_id),
        student_id=str(sub.student_id),
        result_status=(
            ExamResultStatus.PENDING_REVIEW if pending else ExamResultStatus.FINAL
        ),
        is_final=not pending,
        final_total_score=None if pending else total,
        confirmed_subtotal=Decimal(0) if pending else total,
        confirmed_subtotal_label="已确认部分",
        total_max_score=sum(
            (l.score for l in scenario["exam"].exam_question_links), Decimal(0)
        ),
        expected_answer_count=2,
        graded_answer_count=2,
        counted_answer_count=0 if pending else 2,
        pending_review_answer_count=2 if pending else 0,
        items=items,
        aggregated_at=datetime.now(UTC),
    )
    scenario["repository"].save_exam_result(result)
    return result


def set_failure(session, sub, code, *, m4=False):
    checkpoint = (
        {"error_code": code}
        if not m4
        else {
            "kind": "langgraph-grading-state-payload",
            "version": "2",
            "state": {"error": {"error_code": code}},
        }
    )
    session.add(
        WorkflowRun(
            workflow_id=uuid4().hex,
            request_id=uuid4().hex,
            submission_id=sub.id,
            status=WorkflowStatus.FAILED,
            checkpoint=checkpoint,
            resumable=False,
        )
    )
    session.commit()


def test_final_statistics_and_participation_are_real_facts(
    session, analysis_scenario, client_factory
):
    s = analysis_scenario
    save_result(session, s, 0, ["2", "2"])
    save_result(session, s, 1, ["0", "0"])
    save_result(session, s, 2, ["1", "1"], pending=True)
    set_failure(session, s["submissions"][3], "ProviderTimeout")
    set_failure(session, s["submissions"][4], "EXAM_SCORING_BASIS_MISSING", m4=True)
    client = client_factory(s["service"])
    response = client.get(
        f"/api/results/exams/{s['exam'].id}/summary",
        headers=headers(s["teacher"], UserRole.TEACHER),
    )
    assert response.status_code == 200
    b = response.json()
    assert [
        b[k]
        for k in [
            "eligible_count",
            "participated_count",
            "submitted_count",
            "draft_count",
            "not_participated_count",
        ]
    ] == [7, 6, 5, 1, 1]
    assert [
        b[k]
        for k in [
            "final_count",
            "pending_review_submission_count",
            "failed_submission_count",
            "insufficient_evidence_submission_count",
            "unfinished_submission_count",
        ]
    ] == [2, 1, 1, 1, 0]
    assert b["average_of_final_scores"] == "2.00"
    assert b["final_score_distribution"] == [
        {"score": "0.00", "submission_count": 1},
        {"score": "4.00", "submission_count": 1},
    ]
    assert b["distribution_denominator"] == 2
    q1, q2 = b["question_statistics"]
    assert (q1["awarded_score"], q1["maximum_score"], q1["score_rate"]) == (
        "2.00",
        "4.00",
        "0.5000",
    )
    assert (q2["awarded_score"], q2["maximum_score"], q2["lost_score"]) == (
        "2.00",
        "6.00",
        "4.00",
    )
    kp1, kp2 = b["knowledge_point_statistics"]
    assert (
        kp1["awarded_score"],
        kp1["maximum_score"],
        kp1["lost_score"],
        kp1["effective_student_count"],
        kp1["effective_answer_count"],
    ) == ("4.00", "10.00", "6.00", 2, 4)
    assert (kp2["awarded_score"], kp2["maximum_score"], kp2["lost_score"]) == (
        "2.00",
        "6.00",
        "4.00",
    )
    assert "不可加总" in b["knowledge_point_attribution"]
    assert all(
        a["final_lost_score"] is None
        for a in b["attention_students"]
        if a["student_id"] not in {str(s["students"][0].id), str(s["students"][1].id)}
    )
    with session.no_autoflush:
        assert [q.score for q in s["questions"]] == [Decimal(99), Decimal(99)]


def test_zero_observation_returns_null_not_zero_rates(session, analysis_scenario):
    s = analysis_scenario
    b = s["service"].get_exam_summary(str(s["teacher"].id), str(s["exam"].id))
    assert b.final_count == 0 and b.average_of_final_scores is None
    assert b.final_score_distribution == [] and b.statistics_not_ready_reason
    assert all(
        q.awarded_score is None and q.score_rate is None and q.not_ready_reason
        for q in b.question_statistics
    )
    assert all(
        k.lost_score is None and k.maximum_score is None
        for k in b.knowledge_point_statistics
    )


def test_unknown_historical_association_keeps_total_but_excludes_attribution(
    session, analysis_scenario
):
    s = analysis_scenario
    save_result(session, s, 0, ["2", "2"])
    from backend.app.models import GradingResult

    rows = list(session.scalars(select(GradingResult)))
    rows[0].exam_question_id = None
    session.commit()
    b = s["service"].get_exam_summary(str(s["teacher"].id), str(s["exam"].id))
    assert b.final_count == 1 and b.average_of_final_scores == Decimal(4)
    assert b.insufficient_evidence_submission_count == 1
    assert b.question_statistics[0].effective_submission_count == 0
    assert b.question_statistics[0].awarded_score is None
    assert b.knowledge_point_statistics[0].maximum_score == Decimal(3)


def test_current_eligibility_does_not_erase_historical_participation(
    session, analysis_scenario
):
    s = analysis_scenario
    s["students"][0].is_active = False
    session.query(ExamParticipant).filter(
        ExamParticipant.student_id == s["students"][1].id
    ).delete()
    session.commit()
    b = s["service"].get_exam_summary(str(s["teacher"].id), str(s["exam"].id))
    assert (
        b.eligible_count == 5 and b.participated_count == 6 and b.submitted_count == 5
    )
    assert b.not_participated_count == 1


def test_no_assignment_uses_legacy_whole_student_population(session, analysis_scenario):
    s = analysis_scenario
    session.query(ExamParticipant).delete()
    extra = add_student(session, username="whole-open")
    b = s["service"].get_exam_summary(str(s["teacher"].id), str(s["exam"].id))
    assert b.eligible_count == 8 and b.not_participated_count == 2
    assert str(extra.id) in {a.student_id for a in b.attention_students}


def test_recovered_final_is_not_marked_by_an_old_failed_workflow(
    session, analysis_scenario
):
    s = analysis_scenario
    set_failure(session, s["submissions"][0], "ProviderTimeout")
    save_result(session, s, 0, ["2", "3"])
    b = s["service"].get_exam_summary(str(s["teacher"].id), str(s["exam"].id))
    assert b.final_count == 1 and b.failed_submission_count == 0
    assert str(s["students"][0].id) not in {a.student_id for a in b.attention_students}


@pytest.mark.parametrize("role", [UserRole.STUDENT, UserRole.ADMIN, UserRole.TEACHER])
def test_new_statistics_keep_role_and_course_boundary(
    session, analysis_scenario, client_factory, role
):
    s = analysis_scenario
    actor = add_user(session, role, username="foreign", email="foreign@example.com")
    client = client_factory(s["service"])
    path = f"/api/results/exams/{s['exam'].id}/summary"
    assert client.get(path, headers=headers(actor, role)).status_code == 403
    assert client.get(path).status_code == 401


def test_shared_questions_use_each_exam_maximum(session, analysis_scenario):
    s = analysis_scenario
    save_result(session, s, 0, ["2", "2"])
    second = Exam(
        course_id=s["course"].id,
        created_by=s["teacher"].id,
        title="另一场考试",
        status=ExamStatus.PUBLISHED,
    )
    second.exam_question_links = [
        ExamQuestion(
            question=q,
            order_index=i + 1,
            score=Decimal(score),
            base_score=Decimal(99),
            published_knowledge_points=tags,
        )
        for i, (q, score, tags) in enumerate(
            zip(s["questions"], ["4", "6"], [["一次函数"], ["一次函数", "表达与计算"]])
        )
    ]
    session.add(second)
    session.flush()
    sub = Submission(
        exam_id=second.id,
        student_id=s["students"][0].id,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
    )
    session.add(sub)
    session.flush()
    session.add_all(
        Answer(
            submission_id=sub.id,
            question_id=q.id,
            content="A",
            status=AnswerStatus.SUBMITTED,
        )
        for q in s["questions"]
    )
    session.commit()
    other = {**s, "exam": second, "submissions": [sub]}
    save_result(session, other, 0, ["4", "4"])
    first = s["service"].get_exam_summary(str(s["teacher"].id), str(s["exam"].id))
    second_result = s["service"].get_exam_summary(str(s["teacher"].id), str(second.id))
    assert first.knowledge_point_statistics[0].maximum_score == Decimal(5)
    assert second_result.knowledge_point_statistics[0].maximum_score == Decimal(10)
    assert first.average_of_final_scores == Decimal(
        4
    ) and second_result.average_of_final_scores == Decimal(8)


def test_regrading_pending_excludes_previous_final_from_all_aggregates(
    session, analysis_scenario
):
    s = analysis_scenario
    save_result(session, s, 0, ["2", "2"])
    save_result(session, s, 0, ["1", "1"], pending=True)
    b = s["service"].get_exam_summary(str(s["teacher"].id), str(s["exam"].id))
    assert b.final_count == 0 and b.pending_review_submission_count == 1
    assert b.average_of_final_scores is None and b.final_score_distribution == []
    assert all(k.lost_score is None for k in b.knowledge_point_statistics)
