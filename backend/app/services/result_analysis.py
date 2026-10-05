"""T180 deterministic read projections from stored results and frozen exam links."""

from __future__ import annotations

from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.enums import SubmissionStatus, UserRole, WorkflowStatus
from backend.app.models import Exam, ExamParticipant, Submission, User, WorkflowRun
from backend.app.schemas.grading import (
    ExamResultDTO,
    ExamResultStatus,
    FinalScoreFrequencyDTO,
    KnowledgePointStatisticsDTO,
    QuestionResultDTO,
    QuestionScoreStatisticsDTO,
    StudentAttentionDTO,
)

INSUFFICIENT_CODES = frozenset(
    {
        "EXAM_SCORING_BASIS_MISSING",
        "EXAM_SCORING_INPUT_NOT_SUPPORTED",
        "VISION_REVIEW_REQUIRED",
        "VISION_NOT_SUPPORTED",
        "FILE_MISSING",
        "EXAM_REQUIRED_IMAGE_UNAVAILABLE",
        "RETRIEVAL_CONTEXT_EMPTY",
        "RETRIEVAL_SCOPE_NOT_READY",
        "GRADING_CONTEXT_EMPTY",
        "GRADING_MISSING_CONTEXT",
        "GRADING_MISSING_REFERENCE_ANSWER",
    }
)
SUBMITTED_STATUSES = frozenset(
    {
        SubmissionStatus.SUBMITTED,
        SubmissionStatus.GRADED,
        SubmissionStatus.REVIEWED,
    }
)


def published_result_items(
    exam: Exam,
    result: ExamResultDTO,
) -> tuple[list[QuestionResultDTO], list[str]]:
    """Require actual association, fixed maximum and tags; do not infer old identities."""
    links = {str(link.id): link for link in exam.exam_question_links}
    valid: list[QuestionResultDTO] = []
    insufficient: list[str] = []
    seen: set[str] = set()
    for item in result.items:
        link = links.get(item.exam_question_id or "")
        if (
            link is None
            or item.exam_question_id in seen
            or item.question_id != str(link.question_id)
            or item.order != link.order_index
            or link.score is None
            or link.published_knowledge_points is None
            or item.max_score != link.score
            or item.knowledge_points != link.published_knowledge_points
            or not item.counted
            or item.effective_score is None
            or not Decimal(0) <= item.effective_score <= item.max_score
        ):
            insufficient.append(item.answer_id)
            continue
        seen.add(str(link.id))
        valid.append(item)
    return valid, insufficient


def _amounts(scores: list[tuple[Decimal, Decimal]]) -> dict[str, Decimal | None]:
    if not scores:
        return {
            "awarded_score": None,
            "maximum_score": None,
            "lost_score": None,
            "score_rate": None,
        }
    awarded = sum((score for score, _ in scores), Decimal(0))
    maximum = sum((full for _, full in scores), Decimal(0))
    return {
        "awarded_score": awarded,
        "maximum_score": maximum,
        "lost_score": maximum - awarded,
        "score_rate": (awarded / maximum).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        ),
    }


def _workflow_error(run: WorkflowRun | None) -> str | None:
    if run is None:
        return None
    checkpoint = run.checkpoint or {}
    code = checkpoint.get("error_code")
    if not isinstance(code, str):
        payload = checkpoint.get("state")
        # M4 serializer retains the real AgentError in its business payload.
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("error_code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def build_teacher_statistics(
    session: Session,
    exam: Exam,
    submissions: list[Submission],
    results: dict[str, ExamResultDTO | None],
) -> dict[str, Any]:
    """Read-only; submitted states partition by current stored outcome, no LLM."""
    assigned = set(
        session.scalars(
            select(ExamParticipant.student_id).where(ExamParticipant.exam_id == exam.id)
        )
    )
    users = list(session.scalars(select(User).where(User.is_active.is_(True))))
    eligible = {
        user.id
        for user in users
        if (not user.roles or any(role.name == UserRole.STUDENT for role in user.roles))
        and (not assigned or user.id in assigned)
    }
    observed = {item.student_id for item in submissions}
    runs: dict[str, WorkflowRun] = {}
    if submissions:
        for workflow in session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.submission_id.in_([s.id for s in submissions]))
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        ):
            runs.setdefault(str(workflow.submission_id), workflow)
    finals: list[tuple[Submission, ExamResultDTO]] = []
    attention: list[StudentAttentionDTO] = []
    counts = {
        "pending_review_submission_count": 0,
        "failed_submission_count": 0,
        "insufficient_evidence_submission_count": 0,
        "unfinished_submission_count": 0,
    }
    by_link: dict[str, list[tuple[str, QuestionResultDTO]]] = {}
    for submission in submissions:
        sid = str(submission.id)
        result = results.get(sid)
        if submission.status == SubmissionStatus.DRAFT:
            attention.append(
                StudentAttentionDTO(
                    student_id=str(submission.student_id),
                    submission_id=sid,
                    reasons=["已开始但尚未提交。"],
                )
            )
            continue
        run = runs.get(sid)
        code = _workflow_error(run)
        if (
            result is not None
            and result.is_final
            and result.final_total_score is not None
        ):
            finals.append((submission, result))
            valid, insufficient = published_result_items(exam, result)
            losses = [
                item
                for item in valid
                if (item.effective_score or Decimal(0)) < item.max_score
            ]
            for item in valid:
                by_link.setdefault(str(item.exam_question_id), []).append(
                    (str(submission.student_id), item)
                )
            reasons = []
            if losses:
                reasons.append(
                    "存在已确认的最终失分；明细使用本场固定满分和发布知识点。"
                )
            if insufficient or len(valid) != len(exam.exam_question_links):
                counts["insufficient_evidence_submission_count"] += 1
                reasons.append(
                    "部分历史结果缺本场关联/固定知识点依据，不能作知识点归因。"
                )
            if reasons:
                attention.append(
                    StudentAttentionDTO(
                        student_id=str(submission.student_id),
                        submission_id=sid,
                        reasons=reasons,
                        answer_ids=[item.answer_id for item in losses] + insufficient,
                        exam_question_ids=[
                            str(item.exam_question_id) for item in losses
                        ],
                        knowledge_points=list(
                            dict.fromkeys(
                                p for item in losses for p in item.knowledge_points
                            )
                        ),
                        final_lost_score=result.total_max_score
                        - result.final_total_score,
                    )
                )
            continue
        if code in INSUFFICIENT_CODES:
            key, reason = (
                "insufficient_evidence_submission_count",
                "评分依据不足，未形成最终成绩。",
            )
        elif (
            result is not None and result.result_status == ExamResultStatus.FAILED
        ) or (run is not None and run.status == WorkflowStatus.FAILED):
            key, reason = (
                "failed_submission_count",
                "阅卷失败，未记零分或推断知识盲点。",
            )
        elif result is not None and result.pending_review_answer_count:
            key, reason = (
                "pending_review_submission_count",
                "存在待人工复核题目，未形成最终成绩。",
            )
        else:
            key, reason = "unfinished_submission_count", "阅卷尚未完成或尚无结果。"
        counts[key] += 1
        attention.append(
            StudentAttentionDTO(
                student_id=str(submission.student_id),
                submission_id=sid,
                reasons=[reason],
                error_code=code,
            )
        )
    for student_id in sorted(eligible - observed, key=str):
        attention.append(
            StudentAttentionDTO(
                student_id=str(student_id),
                submission_id=None,
                reasons=["当前有分配资格，但尚无开始答卷记录。"],
            )
        )
    questions: list[QuestionScoreStatisticsDTO] = []
    points: dict[str, list[str]] = {}
    for link in sorted(exam.exam_question_links, key=lambda link: link.order_index):
        rows = by_link.get(str(link.id), [])
        questions.append(
            QuestionScoreStatisticsDTO(
                exam_question_id=str(link.id),
                question_id=str(link.question_id),
                order=link.order_index,
                max_score=link.score,
                published_knowledge_points=link.published_knowledge_points,
                effective_submission_count=len(rows),
                excluded_final_submission_count=len(finals) - len(rows),
                **_amounts(
                    [
                        (item.effective_score or Decimal(0), item.max_score)
                        for _, item in rows
                    ]
                ),
                not_ready_reason=None if rows else "暂无具有本场固定依据的最终观测。",
            )
        )
        for point in link.published_knowledge_points or []:
            points.setdefault(point, []).append(str(link.id))
    knowledge = []
    for point, link_ids in points.items():
        rows = [row for link_id in link_ids for row in by_link.get(link_id, [])]
        knowledge.append(
            KnowledgePointStatisticsDTO(
                knowledge_point=point,
                exam_question_ids=link_ids,
                effective_student_count=len({student for student, _ in rows}),
                effective_answer_count=len(rows),
                **_amounts(
                    [
                        (item.effective_score or Decimal(0), item.max_score)
                        for _, item in rows
                    ]
                ),
            )
        )
    distribution = Counter(result.final_total_score for _, result in finals)
    return dict(
        eligible_count=len(eligible),
        participated_count=len(observed),
        draft_count=sum(s.status == SubmissionStatus.DRAFT for s in submissions),
        not_participated_count=len(eligible - observed),
        participation_scope=(
            "当前启用学生的分配资格人口，独立于当前考试开放时间窗；"
            "无分配记录按既有全体开放口径（含无角色旧账号）。"
            "已参加按真实答卷去重，保留历史停用/已撤销分配的观测。"
        ),
        **counts,
        distribution_denominator=len(finals),
        final_score_distribution=[
            FinalScoreFrequencyDTO(score=cast(Decimal, score), submission_count=count)
            for score, count in sorted(distribution.items())
        ],
        question_statistics=questions,
        knowledge_point_statistics=knowledge,
        attention_students=attention,
        statistics_not_ready_reason=(
            None if finals else "暂无最终有效答卷；平均分、分布及得分率暂无数据。"
        ),
    )
