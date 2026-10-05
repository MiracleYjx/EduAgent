"""Frozen T146 references + explicit synthetic execution context, TCR §39.

Expected values come from immutable CSV/JSON, never from business statistics.
Original unknown participation and missing source identities remain unknown.
"""

import csv
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.results import ResultsQueryService
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    SubmissionStatus,
)
from backend.app.models import (
    Answer,
    Course,
    Exam,
    ExamParticipant,
    ExamQuestion,
    Question,
    Submission,
)
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    ExamResultDTO,
    ExamResultStatus,
    QuestionResultDTO,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from tests.contract.test_results_analysis_api import set_failure
from tests.unit.services.test_submission_service import add_student, add_teacher

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "benchmark/corpus/t146-ai-authorized-20261003"
SOURCE = ROOT / "benchmark/corpus/v2-draft-20261001/statistics_cases.json"
CASE_IDS = ("STATS-MIXED", "STATS-EMPTY", "STATS-STARTED")


def frozen_rows(case_id, exam_id):
    with (REFERENCE / "statistics_reference.csv").open(encoding="utf-8") as stream:
        return [
            r
            for r in csv.DictReader(stream)
            if r["case_id"] == case_id and r["exam_fixture_id"] == exam_id
        ]


def frozen_exam(case_id, exam_id):
    reference = json.loads(
        (REFERENCE / "statistics_annotations.json").read_text("utf-8")
    )
    case = next(
        c for c in reference["statistics_annotations"] if c["case_id"] == case_id
    )
    return next(e for e in case["exams"] if e["exam_fixture_id"] == exam_id)


def seed(session, case_id):
    source_path = (
        REFERENCE / "statistics_controls.json" if case_id == "STATS-STARTED" else SOURCE
    )
    source = json.loads(source_path.read_text("utf-8"))
    teacher = add_teacher(session)
    course = Course(name="T184冻结参考运行课程", created_by=teacher.id)
    session.add(course)
    session.flush()
    questions = [
        Question(
            course_id=course.id,
            created_by=teacher.id,
            type=QuestionType.SINGLE_CHOICE if i == 0 else QuestionType.SHORT_ANSWER,
            content=f"T184执行上下文题{i+1}（不是原标注题干）",
            reference_answer="运行夹具参考答案",
            scoring_rubric="运行夹具评分依据；只验统计链路，不评模型质量。",
            score=Decimal(99),
            status=QuestionStatus.APPROVED,
        )
        for i in range(2)
    ]
    session.add_all(questions)
    session.commit()
    students = [add_student(session, username=f"t184_s{i}") for i in range(6)]
    repository = DatabaseGradingRepository(
        session_factory=lambda: Session(session.get_bind())
    )
    service = ResultsQueryService(session=session, repository=repository)
    scenarios = []
    for src in source["exams"]:
        exam = Exam(
            course_id=course.id,
            created_by=teacher.id,
            title=f"T184-{case_id}-{src['exam_fixture_id']}",
            status=ExamStatus.PUBLISHED,
        )
        exam.exam_question_links = [
            ExamQuestion(
                question=q,
                order_index=i + 1,
                score=Decimal(raw["max_score"]),
                base_score=Decimal(99),
                published_knowledge_points=raw["knowledge_points"],
            )
            for i, (q, raw) in enumerate(zip(questions, src["questions"], strict=True))
        ]
        session.add(exam)
        session.flush()
        session.add_all(
            ExamParticipant(exam_id=exam.id, student_id=s.id) for s in students
        )
        scenario = {
            "teacher": teacher,
            "course": course,
            "questions": questions,
            "students": students,
            "exam": exam,
            "repository": repository,
            "service": service,
            "submissions": {},
            "fixture_id": src["exam_fixture_id"],
        }
        for raw in src["submissions"]:
            if case_id == "STATS-EMPTY" and raw["state"] == "Final":
                continue  # isolated selected-population view, never the original whole exam
            index = int(raw["student_fixture_id"][1:]) - 1
            sub = Submission(
                exam_id=exam.id,
                student_id=students[index].id,
                status=SubmissionStatus.SUBMITTED,
                submitted_at=datetime.now(UTC),
            )
            session.add(sub)
            session.flush()
            session.add_all(
                Answer(
                    submission_id=sub.id,
                    question_id=q.id,
                    content=f"受控执行答案-{raw['student_fixture_id']}-{i+1}",
                    status=AnswerStatus.SUBMITTED,
                )
                for i, q in enumerate(questions)
            )
            session.commit()
            scenario["submissions"][raw["student_fixture_id"]] = sub
            if raw["state"] in ("Final", "Pending Review"):
                pending = raw["state"] == "Pending Review"
                # Original pending score unknown; synthetic candidates are not final/labels.
                write_result(
                    session, scenario, sub, raw["scores"] or ["1", "1"], pending=pending
                )
            else:
                code = (
                    "ProviderTimeout"
                    if raw["state"] == "Failed"
                    else "EXAM_SCORING_BASIS_MISSING"
                )
                set_failure(session, sub, code, m4=True)
        scenarios.append(scenario)
    return scenarios


def write_result(session, s, sub, scores, *, pending=False):
    answers = {
        a.question_id: a
        for a in session.scalars(select(Answer).where(Answer.submission_id == sub.id))
    }
    items = [
        QuestionResultDTO(
            order=link.order_index,
            answer_id=str(answers[link.question_id].id),
            question_id=str(link.question_id),
            exam_question_id=str(link.id),
            submission_id=str(sub.id),
            question_type=link.question.type,
            max_score=link.score,
            score=Decimal(score),
            effective_score=None if pending else Decimal(score),
            counted=not pending,
            requires_review=pending,
            grading_status="Pending Review" if pending else "Accepted",
            review_status="Pending Review" if pending else "Not Required",
            validation_status="Validated",
            knowledge_points=link.published_knowledge_points,
            reason="受控合成执行评分解释；非教师标注或模型实测。",
            decision=(
                ConfidenceDecisionDTO(
                    confidence=0.1 if pending else 0.95,
                    threshold=0.8,
                    requires_review=pending,
                    review_status="Pending Review" if pending else "Not Required",
                    grading_status="Pending Review" if pending else "Accepted",
                    reason="受控执行决策夹具，非模型置信度测量",
                )
                if link.question.type == QuestionType.SHORT_ANSWER
                else None
            ),
        )
        for link, score in zip(s["exam"].exam_question_links, scores, strict=True)
    ]
    total = sum((Decimal(x) for x in scores), Decimal(0))
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
            (l.score for l in s["exam"].exam_question_links), Decimal(0)
        ),
        expected_answer_count=2,
        graded_answer_count=2,
        counted_answer_count=0 if pending else 2,
        pending_review_answer_count=2 if pending else 0,
        items=items,
        aggregated_at=datetime.now(UTC),
    )
    s["repository"].save_exam_result(result)
    return result


def metric_values(body, case_id):
    """Project only actual API facts; extra evaluation arithmetic is explicit."""
    fields = {
        "eligible": "eligible_count",
        "submitted": "submitted_count",
        "valid_final": "final_count",
        "pending_review": "pending_review_submission_count",
        "failed": "failed_submission_count",
        "insufficient_evidence": "insufficient_evidence_submission_count",
        "participated": "participated_count",
    }
    values = {k: body[v] for k, v in fields.items()}
    if case_id == "STATS-EMPTY":
        values["selected_observed_submissions"] = body["submitted_count"]
        # Original population count is a source-only fact, not emitted by selected API.
    else:
        values["no_submission"] = body["eligible_count"] - body["submitted_count"]
    full = sum(
        (Decimal(q["max_score"]) for q in body["question_statistics"]), Decimal(0)
    )
    values.update(
        mean=body["average_of_final_scores"],
        exam_full_score=full,
        raw_exact_score_distribution={
            x["score"]: x["submission_count"] for x in body["final_score_distribution"]
        },
    )
    numerator = sum(
        (
            Decimal(x["score"]) * x["submission_count"]
            for x in body["final_score_distribution"]
        ),
        Decimal(0),
    )
    denom = full * body["final_count"]
    values["whole_exam_rate"] = numerator / denom if denom else None
    q_loss = []
    k_loss = []
    for q, name in zip(
        body["question_statistics"], ["shared-choice", "shared-short"], strict=True
    ):
        values[f"question_{name}"] = q["score_rate"]
        values[f"question_loss_{name}"] = q["lost_score"]
        q_loss.append(q["lost_score"])
    for k in body["knowledge_point_statistics"]:
        values[f"knowledge_{k['knowledge_point']}"] = k["score_rate"]
        values[f"knowledge_loss_{k['knowledge_point']}"] = k["lost_score"]
        k_loss.append(k["lost_score"])
    whole = (
        sum((Decimal(x) for x in q_loss), Decimal(0)) if body["final_count"] else None
    )
    overlap = (
        sum((Decimal(x) for x in k_loss), Decimal(0)) if body["final_count"] else None
    )
    values.update(
        whole_exam_lost_sum=whole,
        knowledge_lost_sum_if_wrongly_added=overlap,
        duplicate_overlap_loss=overlap - whole if whole is not None else None,
    )
    bins = [0, 0, 0]
    for x in body["final_score_distribution"]:
        rate = Decimal(x["score"]) / full
        bins[0 if rate < Decimal(".6") else 1 if rate < Decimal(".8") else 2] += x[
            "submission_count"
        ]
    for name, n in zip(
        ["0_to_below_60_percent", "60_to_below_80_percent", "80_to_100_percent"],
        bins,
        strict=True,
    ):
        values[f"distribution_{name}"] = n if body["final_count"] else None
    return values


def compare(body, case_id, exam_id):
    actual = metric_values(body, case_id)
    checks = []
    for row in frozen_rows(case_id, exam_id):
        metric = row["metric"]
        if row["label_state"] == "unknown" or metric == "original_submitted_population":
            checks.append(
                {
                    "metric": metric,
                    "state": (
                        "unknown" if row["label_state"] == "unknown" else "source_only"
                    ),
                    "expected": row["value"],
                    "actual": None,
                }
            )
            continue
        got = actual[metric]
        expected = row["value"]
        if metric == "raw_exact_score_distribution":
            expected = json.loads(expected)
            match = got == expected
        elif row["label_state"] == "no_data":
            expected = None
            match = got is None
        else:
            expected = Decimal(expected)
            # Public DTO ratio uses four decimal places. Do not compare unrounded 1/3.
            if row["unit"] == "ratio" and metric.startswith(
                ("question_", "knowledge_")
            ):
                expected = expected.quantize(Decimal("0.0001"))
            match = got is not None and Decimal(str(got)) == expected
        checks.append(
            {
                "metric": metric,
                "state": "matched" if match else "mismatch",
                "expected": expected,
                "actual": got,
            }
        )
    return checks


def save_evidence(name, body):
    root = os.environ.get("T184_EVIDENCE_DIR")
    if root:
        target = Path(root)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{name}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2, default=str), "utf-8"
        )
