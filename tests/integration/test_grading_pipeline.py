"""T058 混合答卷阅卷与诊断集成测试（真实装配 + mock Provider + 真实持久化）。

测试使用**真实应用装配**：真实仓储（``DatabaseGradingRepository``）、真实答卷快照读取器、
真实评分管道（``DefaultScoringPipeline`` + ``subjective_pipeline`` 适配的 T052 评分器与
T053 置信度策略）、真实诊断存储与生成入口；仅替换外部依赖（检索候选、Embedding、LLM Provider）。

数据落在**真实 PostgreSQL 的隔离 schema**（``tests/postgres_helpers.isolated_postgres_engine``）
中，事务与约束行为由真实数据库验证，不会改动既有业务表。PostgreSQL 或 pgvector 不可用时
按既有约定明确跳过并给出原因，不静默伪装成通过。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import (
    Answer,
    Course,
    DiagnosisReport,
    Exam,
    ExamResult,
    GradingResult,
    Question,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.schemas.ai import GradingResult as GradingResultPayload
from backend.app.services.diagnosis_service import DiagnosisService, LearningSuggestions
from backend.app.services.grading.diagnosis_report_store import (
    DiagnosisRecorder,
    DiagnosisReportStore,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    DefaultScoringPipeline,
    GradingTaskService,
    InlineGradingTaskExecutor,
)
from backend.app.services.grading.subjective_pipeline import build_subjective_scorer
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.subjective_grading_doubles import (
    StubEmbeddingProvider,
    StubReranker,
    StubRetriever,
    StubScoringProvider,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings


class StubSuggestionProvider:
    """诊断学习建议 Provider 替身；不调用真实模型。"""

    def __init__(self, suggestions: Sequence[str] = ("复习变量的引用方式。",)) -> None:
        self.suggestions = list(suggestions)
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[LearningSuggestions],
        **kwargs: Any,
    ) -> LearningSuggestions:
        self.calls.append({"messages": list(messages), "schema": schema, "kwargs": kwargs})
        return schema(suggestions=list(self.suggestions))


@pytest.fixture
def engine() -> Iterator[Engine]:
    """真实 PostgreSQL 隔离 schema 引擎。"""

    with isolated_postgres_engine() as active:
        yield active


@pytest.fixture
def mixed_scenario(engine: Engine) -> dict[str, Any]:
    """一份含一道客观题与两道主观题的已提交答卷。"""

    with Session(engine) as session:
        teacher = User(
            username="t058-teacher",
            email="t058-teacher@example.com",
            password_hash="hashed-password",
        )
        teacher.roles.append(_role(session, UserRole.TEACHER))
        student = User(
            username="t058-student",
            email="t058-student@example.com",
            password_hash="hashed-password",
        )
        student.roles.append(_role(session, UserRole.STUDENT))
        course = Course(name="T058 课程", creator=teacher)
        objective = Question(
            course=course,
            creator=teacher,
            type=QuestionType.SINGLE_CHOICE,
            content="下列哪个是不可变类型？",
            options=["tuple", "list"],
            reference_answer="tuple",
            knowledge_points=["数据类型"],
            score=Decimal("10.00"),
            status=QuestionStatus.APPROVED,
        )
        subjective_one = _subjective_question(course, teacher, "解释变量的作用。", ["变量"])
        subjective_two = _subjective_question(course, teacher, "解释作用域。", ["作用域"])
        exam = Exam(
            course=course,
            creator=teacher,
            title="T058 混合答卷测验",
            questions=[objective, subjective_one, subjective_two],
            status=ExamStatus.PUBLISHED,
        )
        submission = Submission(
            exam=exam,
            student=student,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=datetime.now(UTC),
        )
        answers = [
            Answer(
                submission=submission,
                question=question,
                content="tuple" if question is objective else "变量用于保存数据。",
                status=AnswerStatus.SUBMITTED,
            )
            for question in (objective, subjective_one, subjective_two)
        ]
        session.add(submission)
        session.commit()
        return {
            "teacher_id": str(teacher.id),
            "student_id": str(student.id),
            "course_id": str(course.id),
            "submission_id": str(submission.id),
            "objective_answer_id": str(answers[0].id),
            "subjective_answer_ids": [str(answers[1].id), str(answers[2].id)],
        }


def _role(session: Session, role: UserRole) -> Any:
    """复用既有角色行，避免重复插入。"""

    from backend.app.models import Role

    existing = session.scalars(select(Role).where(Role.name == role)).one_or_none()
    if existing is not None:
        return existing
    role_row = Role(name=role)
    session.add(role_row)
    session.flush()
    return role_row


def _subjective_question(course: Course, teacher: User, content: str, points: list[str]) -> Question:
    """构造可用于主观题评分的问题。"""

    return Question(
        course=course,
        creator=teacher,
        type=QuestionType.SHORT_ANSWER,
        content=content,
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明保存和引用数据即可。",
        knowledge_points=points,
        score=Decimal("10.00"),
        status=QuestionStatus.APPROVED,
    )


def _build_service(
    engine: Engine,
    *,
    score_confidence: float,
    suggestion_provider: StubSuggestionProvider,
) -> tuple[GradingTaskService, StubScoringProvider, StubRetriever, StubEmbeddingProvider]:
    """按生产装配构造任务服务；只替换外部依赖。"""

    settings = build_test_settings(confidence_threshold=0.8)
    session_factory = lambda: Session(engine)
    repository = DatabaseGradingRepository(session_factory=session_factory)
    reader = DatabaseGradingSubmissionReader(session_factory=session_factory)
    provider = StubScoringProvider(score=6.0, confidence=score_confidence)
    retriever = StubRetriever([make_chunk()])
    embedding = StubEmbeddingProvider()
    pipeline = DefaultScoringPipeline(
        subjective_scorer=build_subjective_scorer(
            session_factory=session_factory,
            settings=settings,
            provider=provider,
            retriever=retriever,
            reranker=StubReranker(),
            embedding_provider=embedding,
        )
    )
    executor = InlineGradingTaskExecutor(
        repository=repository,
        reader=reader,
        pipeline=pipeline,
        diagnosis_recorder=DiagnosisRecorder(
            store=DiagnosisReportStore(session_factory=session_factory),
            service=DiagnosisService(provider=suggestion_provider),
        ),
    )
    return (
        GradingTaskService(repository=repository, reader=reader, executor=executor),
        provider,
        retriever,
        embedding,
    )


def test_mixed_submission_runs_full_pipeline_and_forms_final_diagnosis(
    engine: Engine,
    mixed_scenario: dict[str, Any],
) -> None:
    """客观题确定性评分、主观题检索+结构化评分落库，全部接受后形成最终成绩与诊断。"""

    suggestions = StubSuggestionProvider()
    service, provider, retriever, embedding = _build_service(
        engine, score_confidence=0.9, suggestion_provider=suggestions
    )

    task = service.trigger(
        mixed_scenario["submission_id"],
        teacher_id=mixed_scenario["teacher_id"],
        request_id="request-t058",
    )

    stored_task = service.get_task(
        task.task_id, teacher_id=mixed_scenario["teacher_id"]
    )
    assert stored_task.status.value == "Completed"
    assert stored_task.durable is True
    assert stored_task.is_final is True
    assert stored_task.pending_review_answer_count == 0

    # 客观题走确定性评分：主观题评分调用次数等于主观题数量，向量与重排各调用一次/题。
    assert len(provider.calls) == len(mixed_scenario["subjective_answer_ids"])
    assert len(retriever.calls) == len(mixed_scenario["subjective_answer_ids"])
    assert len(embedding.queries) == len(mixed_scenario["subjective_answer_ids"])

    with Session(engine) as session:
        rows = {
            str(row.answer_id): row
            for row in session.scalars(select(GradingResult))
        }
        objective = rows[mixed_scenario["objective_answer_id"]]
        assert objective.score == Decimal("10.00")
        assert objective.confidence == 1.0
        assert objective.decision_confidence is None

        for answer_id in mixed_scenario["subjective_answer_ids"]:
            row = rows[answer_id]
            assert row.score == Decimal("6.00")
            assert row.confidence == 0.9
            assert row.decision_threshold == 0.8
            assert row.decision_requires_review is False
            assert row.review_status.value == "Not Required"
            assert row.retrieved_context_ids == ["chunk-1"]

        exam_result = session.scalars(select(ExamResult)).one()
        assert exam_result.is_final is True
        assert exam_result.confirmed_subtotal == Decimal("22.00")
        assert exam_result.total_max_score == Decimal("30.00")

        report = session.scalars(select(DiagnosisReport)).one()
        assert report.status.value == "Ready"
        assert report.source_exam_result_updated_at is not None
        assert str(report.exam_result_id) == str(exam_result.id)
        assert report.learning_suggestions == ["复习变量的引用方式。"]
        workflow = session.scalars(select(WorkflowRun)).one()
        assert workflow.status.value == "Completed"
        assert workflow.request_id == "request-t058"
        assert (workflow.checkpoint or {})["answer_order"] == [
            mixed_scenario["objective_answer_id"],
            *mixed_scenario["subjective_answer_ids"],
        ]

    # 新 Session 重读：题序、决策快照与汇总时间与当次写入一致。
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    reread = repository.get_exam_result(mixed_scenario["submission_id"])
    assert reread is not None
    assert [item.answer_id for item in reread.items] == [
        mixed_scenario["objective_answer_id"],
        *mixed_scenario["subjective_answer_ids"],
    ]
    subjective_item = reread.items[1]
    assert subjective_item.decision is not None
    assert subjective_item.decision.threshold == 0.8
    assert subjective_item.counted is True
    single = repository.get_single_result(
        mixed_scenario["submission_id"], mixed_scenario["subjective_answer_ids"][0]
    )
    assert single is not None
    assert single.retrieved_context_ids == ["chunk-1"]


def test_low_confidence_subjective_stays_pending_and_blocks_diagnosis(
    engine: Engine,
    mixed_scenario: dict[str, Any],
) -> None:
    """低置信度主观题进入待复核、不计入最终总分，且诊断不提前生成。"""

    suggestions = StubSuggestionProvider()
    service, provider, _, _ = _build_service(
        engine, score_confidence=0.5, suggestion_provider=suggestions
    )

    task = service.trigger(
        mixed_scenario["submission_id"],
        teacher_id=mixed_scenario["teacher_id"],
    )

    stored_task = service.get_task(
        task.task_id, teacher_id=mixed_scenario["teacher_id"]
    )
    assert stored_task.status.value == "Completed"
    assert stored_task.is_final is False
    assert stored_task.pending_review_answer_count == 2
    assert len(provider.calls) == 2

    with Session(engine) as session:
        exam_result = session.scalars(select(ExamResult)).one()
        assert exam_result.is_final is False
        assert exam_result.final_total_score is None
        assert exam_result.confirmed_subtotal == Decimal("10.00")
        assert session.scalars(select(WorkflowRun)).one().status.value == "Paused"
        assert list(session.scalars(select(DiagnosisReport))) == []

    assert suggestions.calls == []

    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    reread = repository.get_exam_result(mixed_scenario["submission_id"])
    assert reread is not None
    pending = reread.items[1]
    assert pending.requires_review is True
    assert pending.counted is False
    assert pending.effective_score is None
    assert pending.decision is not None
    assert pending.decision.requires_review is True

    store = DiagnosisReportStore(session_factory=lambda: Session(engine))
    not_ready = store.read(mixed_scenario["submission_id"], reread)
    assert not_ready.status.value == "Not Ready"
    assert not_ready.learning_suggestions == []


def test_existing_report_becomes_stale_after_regrade(
    engine: Engine,
    mixed_scenario: dict[str, Any],
) -> None:
    """重新汇总后旧诊断过期，不被旧结果覆盖。"""

    service, _, _, _ = _build_service(
        engine, score_confidence=0.9, suggestion_provider=StubSuggestionProvider()
    )
    service.trigger(
        mixed_scenario["submission_id"], teacher_id=mixed_scenario["teacher_id"]
    )
    store = DiagnosisReportStore(session_factory=lambda: Session(engine))
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    with Session(engine) as session, session.begin():
        row = session.scalars(select(ExamResult)).one()
        row.aggregated_at = row.aggregated_at + timedelta(minutes=1)

    result = repository.get_exam_result(mixed_scenario["submission_id"])
    assert result is not None
    stale = store.read(mixed_scenario["submission_id"], result)

    assert stale.status.value == "Stale"
    assert stale.generated_at is not None
    with Session(engine) as session:
        assert len(list(session.scalars(select(DiagnosisReport)))) == 1


def test_partial_write_failure_leaves_no_intermediate_result(
    engine: Engine,
    mixed_scenario: dict[str, Any],
) -> None:
    """真实数据库上的整批事务：跨答卷写入失败时整体回滚，不留中间态。"""

    service, _, _, _ = _build_service(
        engine, score_confidence=0.9, suggestion_provider=StubSuggestionProvider()
    )
    service.trigger(
        mixed_scenario["submission_id"], teacher_id=mixed_scenario["teacher_id"]
    )
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    good = repository.get_exam_result(mixed_scenario["submission_id"])
    assert good is not None
    foreign = _foreign_payload(engine, mixed_scenario)

    from backend.app.services.grading.grading_task_service import (
        GradingOutcome,
        GradingResultOwnershipError,
    )

    with pytest.raises(GradingResultOwnershipError):
        repository.save_outcome(
            mixed_scenario["submission_id"],
            GradingOutcome(
                results=(foreign,),
                decisions={},
                exam_result=good,
            ),
            task_id="task-foreign",
        )

    with Session(engine) as session:
        assert len(list(session.scalars(select(WorkflowRun)))) == 1
        assert len(list(session.scalars(select(GradingResult)))) == 3


def _foreign_payload(
    engine: Engine,
    mixed_scenario: dict[str, Any],
) -> GradingResultPayload:
    """构造一条属于其它答卷的评分结果（用于验证事务回滚）。"""

    with Session(engine) as session:
        exam = session.get(Exam, _exam_id(engine, mixed_scenario["submission_id"]))
        assert exam is not None
        other_student = User(
            username="t058-other",
            email="t058-other@example.com",
            password_hash="hashed-password",
        )
        other = Submission(
            exam_id=exam.id,
            student=other_student,
            status=SubmissionStatus.SUBMITTED,
        )
        session.add(other)
        session.flush()
        answer = Answer(
            submission=other,
            question_id=exam.questions[0].id,
            content="tuple",
            status=AnswerStatus.SUBMITTED,
        )
        session.add(answer)
        session.commit()
        return GradingResultPayload(
            question_type=QuestionType.SINGLE_CHOICE,
            score=10.0,
            max_score=10.0,
            reason="参考答案一致。",
            correct_points=["tuple 是不可变类型"],
            missing_knowledge_points=[],
            knowledge_points=["数据类型"],
            suggestions=["继续保持。"],
            confidence=1.0,
            validation_status="Validated",
            review_status="Not Required",
            retrieved_context_ids=[],
            answer_id=str(answer.id),
            submission_id=mixed_scenario["submission_id"],
        )


def _exam_id(engine: Engine, submission_id: str) -> Any:
    """返回答卷对应的考试标识。"""

    from uuid import UUID

    with Session(engine) as session:
        return session.scalars(
            select(Submission.exam_id).where(Submission.id == UUID(submission_id))
        ).one()


__all__ = [
    "StubSuggestionProvider",
    "create_engine",
]
