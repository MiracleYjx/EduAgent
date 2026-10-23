"""T076 Workflow API 契约测试：启动幂等、角色差异、唯一恢复入口与未就绪 503。

TCR（2026-09-18，T076 / 评审 B03～B07、B11、遗漏2、遗漏3）：

- **既有覆盖缺口**：T072/T073/T074 已各自验证图、持久检查点与复核服务，但没有真实责任方把
  “启动运行 → 落运行记录 → 状态查询 → 恢复”串起来；也没有测试证明重复启动不产生第二个活动运行、
  学生看不到内部状态、人工复核暂停不能用裸恢复绕过教师结论、依赖缺失时在产生副作用之前返回 503。
- **新增用例**：权限边界（未认证/学生/管理员/跨课程教师）、启动正常路径（真实
  ``WorkflowService`` + 真实 ``DatabaseCheckpointSaver`` + 真实 T060 快照读取，外部 Agent/诊断用替身）、
  重复启动幂等复用、非法答卷状态 409、历史终态不阻挡 Submitted 与显式重评拒绝、教师/学生状态字段差异、
  人工复核暂停缺教师结论 409、已记录结论的恢复回执（含“已保存待继续”部分成功）、
  无可恢复检查点 409、依赖缺失 503 且零副作用、路由已注册。
- **对外契约**：plan §5/§5.2、FR-029～FR-038、T060 快照读取、T065 状态载荷、T071 复核状态契约、
  T072 ``run_async``/``resume_async``/``apply_teacher_decision_async``、T073 ``WorkflowCheckpointStore``/
  ``DatabaseCheckpointSaver``、T074 ``ReviewService.resume_recorded_decision_async``。

本文件只断言对外契约，不复制服务内部实现。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.ai.agents.invocation import AgentInvocation
from backend.app.ai.agents.state import AgentInput, AgentOutput, AgentStatus, AgentType
from backend.app.api.workflow import (
    WorkflowService,
    get_workflow_service,
)
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    QuestionType,
    ReviewStatus,
    UserRole,
    ValidationStatus,
)
from backend.app.models import (
    GradingResult,
    ReviewRecord,
    User,
    WorkflowRun,
)
from backend.app.schemas.ai import GradingResult as GradingResultPayload
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
)
from backend.app.services.auth_service import create_access_token
from backend.app.services.grading.grading_repository import (
    DatabaseGradingRepository,
)
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.review_service import ReviewOutcome
from backend.app.services.workflow_checkpoint import (
    WorkflowCheckpointStore,
)
from tests.support.application_lifecycle import bind_startup_session_factory
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.support.retained_component_app import component_app as create_app
from tests.unit.models.sqlite_support import seed_submission
from tests.unit.services.test_submission_service import add_user
from tests.unit.settings_helpers import build_test_settings

FIXED_NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


class _StubGradingAgent:
    """逐题返回预置评分：客观题自动接受，主观题低置信度进入待复核。"""

    def __init__(self, *, submission_id: str) -> None:
        self.submission_id = submission_id
        self.calls: list[str] = []

    async def grade_answer_async(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        request_id: str,
        workflow_id: str | None = None,
        session: Any = None,
        settings: Any = None,
    ) -> AgentInvocation:
        del session, settings
        self.calls.append(target.answer_id)
        if target.question_type is QuestionType.SINGLE_CHOICE:
            score, confidence, needs_review = 10.0, 1.0, False
        else:
            score, confidence, needs_review = 6.0, 0.3, True
        result = GradingResultPayload(
            question_type=target.question_type,
            score=score,
            max_score=float(target.max_score),
            reason="说明了变量的作用。",
            correct_points=["保存数据"],
            missing_knowledge_points=["引用数据"],
            knowledge_points=list(target.knowledge_points),
            suggestions=["补充变量引用。"],
            confidence=confidence,
            validation_status=ValidationStatus.VALIDATED.value,
            review_status=(
                ReviewStatus.PENDING_REVIEW.value
                if needs_review
                else ReviewStatus.NOT_REQUIRED.value
            ),
            answer_id=target.answer_id,
            submission_id=self.submission_id,
            exam_question_id=(
                target.scoring_input.exam_question_id if target.scoring_input else None
            ),
        )
        decision = ConfidenceDecisionDTO(
            confidence=confidence,
            threshold=0.8,
            requires_review=needs_review,
            review_status=(
                ReviewStatus.PENDING_REVIEW.value
                if needs_review
                else ReviewStatus.NOT_REQUIRED.value
            ),
            grading_status="Pending Review" if needs_review else "Accepted",
            reason="置信度低于阈值，已进入人工复核队列。" if needs_review else "自动接受。",
        )
        return AgentInvocation(
            request_id=request_id,
            workflow_id=workflow_id,
            input=AgentInput(
                agent_type=AgentType.GRADING,
                request_id=request_id,
                workflow_id=workflow_id,
                submission_context=snapshot.to_context(),
                answer_id=target.answer_id,
                question_id=target.question_id,
                question_type=target.question_type,
                scoring_input=target.scoring_input,
            ),
            output=AgentOutput(
                agent_type=AgentType.GRADING,
                status=AgentStatus.PENDING_REVIEW if needs_review else AgentStatus.SUCCESS,
                validation_status=ValidationStatus.VALIDATED,
                question_type=target.question_type,
                grading_result=result,
                confidence=confidence,
                confidence_decision=decision,
                requires_review=needs_review,
            ),
        )

    def score(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("图必须逐题调用 grade_answer_async，不得调用整卷 score。")


class _AcceptedSubjectiveAgent(_StubGradingAgent):
    """把主观题也判为自动接受的替身：用于验证高置信度运行的最终成绩落库。"""

    async def grade_answer_async(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        request_id: str,
        workflow_id: str | None = None,
        session: Any = None,
        settings: Any = None,
    ) -> AgentInvocation:
        invocation = await super().grade_answer_async(
            snapshot,
            target,
            request_id=request_id,
            workflow_id=workflow_id,
            session=session,
            settings=settings,
        )
        if target.question_type is QuestionType.SINGLE_CHOICE:
            return invocation
        graded = invocation.output.grading_result
        assert graded is not None
        accepted = graded.model_copy(
            update={
                "confidence": 0.95,
                "review_status": ReviewStatus.NOT_REQUIRED.value,
            }
        )
        decision = ConfidenceDecisionDTO(
            confidence=0.95,
            threshold=0.8,
            requires_review=False,
            review_status=ReviewStatus.NOT_REQUIRED.value,
            grading_status="Accepted",
            reason="自动接受。",
        )
        output = invocation.output.model_copy(
            update={
                "status": AgentStatus.SUCCESS,
                "requires_review": False,
                "confidence": 0.95,
                "confidence_decision": decision,
                "grading_result": accepted,
            }
        )
        return replace(invocation, output=output)


class _StubDiagnosisService:
    """T072 诊断节点依赖替身。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def generate(self, exam_result: Any) -> DiagnosisReportDTO:
        self.calls.append(exam_result)
        return DiagnosisReportDTO(
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=FIXED_NOW,
            source_exam_result_updated_at=exam_result.aggregated_at,
        )


class _StubReviewService:
    """T074 复核服务替身：只实现工作流恢复所需契约。"""

    def __init__(self, outcome: ReviewOutcome) -> None:
        self.outcome = outcome
        self.calls: list[Any] = []

    async def resume_recorded_decision_async(
        self,
        decision: Any,
        *,
        actor_id: str,
        actor_role: UserRole | str,
    ) -> ReviewOutcome:
        self.calls.append({"decision": decision, "actor_id": actor_id, "actor_role": actor_role})
        return self.outcome


def _file_engine(path: Path) -> Any:
    """构造启用外键约束的文件型 SQLite；跨会话共享同一份数据。"""

    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def env(tmp_path: Path) -> Generator[dict[str, Any], None, None]:
    """隔离数据库 + 真实答卷夹具（教师、课程、题目、已提交答卷）。"""

    engine = _file_engine(tmp_path / "workflow.db")
    with Session(engine) as session:
        fixture = seed_submission(session)
        confirm_synthetic_exam_basis(session, fixture.exam_id)
        session.commit()
        yield {"engine": engine, "fixture": fixture}
    engine.dispose()


def _store(env: dict[str, Any]) -> WorkflowCheckpointStore:
    """构造使用独立会话的检查点存储。"""

    return WorkflowCheckpointStore(session_factory=lambda: Session(env["engine"]))


def _repository(env: dict[str, Any]) -> DatabaseGradingRepository:
    """构造使用独立会话的真实结果仓储（M4 结果事务写入的事实源）。"""

    return DatabaseGradingRepository(session_factory=lambda: Session(env["engine"]))


def _service(env: dict[str, Any], **overrides: Any) -> WorkflowService:
    """构造真实工作流服务：真实存储、快照读取与结果仓储，外部 Agent/诊断/复核用替身。"""

    fixture = env["fixture"]
    kwargs: dict[str, Any] = {
        "checkpoints": _store(env),
        "reader": DatabaseGradingSubmissionReader(
            session_factory=lambda: Session(env["engine"])
        ),
        "session_factory": lambda: Session(env["engine"]),
        "agent": _StubGradingAgent(submission_id=str(fixture.submission_id)),
        "diagnosis_service": _StubDiagnosisService(),
        "repository": _repository(env),
        "settings": build_test_settings(confidence_threshold=0.8),
    }
    kwargs.update(overrides)
    return WorkflowService(**kwargs)


def _headers(user: User, role: UserRole) -> dict[str, str]:
    """签发指定角色的访问令牌。"""

    token = create_access_token(
        user.id,
        secret_key=build_test_settings().JWT_SECRET_KEY,
        roles=[role],
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client_factory(env: dict[str, Any]) -> Generator[Any, None, None]:
    """装配可注入工作流服务的 TestClient 工厂。"""

    with ExitStack() as stack:
        startup_patch = stack.enter_context(pytest.MonkeyPatch.context())
        bind_startup_session_factory(
            startup_patch, sessionmaker(bind=env["engine"])
        )

        def factory(service: WorkflowService | None = None) -> TestClient:
            app = create_app(settings=build_test_settings(), routers=('workflow', 'reviews', 'grading', 'results'))

            def database() -> Generator[Session, None, None]:
                with Session(env["engine"]) as session:
                    yield session

            app.dependency_overrides[get_db] = database
            if service is not None:
                app.dependency_overrides[get_workflow_service] = lambda: service
            return stack.enter_context(TestClient(app))

        yield factory


def _users(env: dict[str, Any]) -> dict[str, User]:
    """读取夹具中的教师与学生账号。"""

    with Session(env["engine"]) as session:
        teacher = session.get(User, env["fixture"].teacher_id)
        student = session.get(User, env["fixture"].student_id)
        assert teacher is not None and student is not None
        return {"teacher": teacher, "student": student}


def _add_user(env: dict[str, Any], *, username: str, role: UserRole) -> User:
    """新增带指定角色的账号（跨课程/越权用例）。"""

    with Session(env["engine"]) as session:
        return add_user(
            session, role, username=username, email=f"{username}@example.com"
        )


def _runs(env: dict[str, Any]) -> list[WorkflowRun]:
    """读取全部运行记录。"""

    with Session(env["engine"]) as session:
        return list(session.scalars(select(WorkflowRun).order_by(WorkflowRun.created_at)))


def _start(client: TestClient, env: dict[str, Any], **payload: Any) -> Any:
    """调用启动端点。"""

    submission_id = str(env["fixture"].submission_id)
    return client.post(
        f"/api/workflow/submissions/{submission_id}/runs",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json={"regrade": False, **payload},
    )


# ---------------------------------------------------------------- 权限与路由










# ---------------------------------------------------------------- 启动




















# ---------------------------------------------------------------- 状态查询角色差异






# ---------------------------------------------------------------- 恢复








# ---------------------------------------------------------------- 未就绪








def _record_decision(env: dict[str, Any], *, reviewer_id: Any) -> ReviewRecord:
    """写入一条已落库的教师结论（复核记录 + 对应评分行）。

    P1.2 之后正式启动入口已经写入同一答卷的评分行，因此这里按 ``answer_id`` 就地复用既有行，
    不再无条件插入第二行（重复插入会与唯一约束冲突）。
    """

    fixture = env["fixture"]
    with Session(env["engine"]) as session:
        grading = session.scalars(
            select(GradingResult).where(
                GradingResult.answer_id == fixture.subjective_answer_id
            )
        ).one_or_none()
        if grading is None:
            grading = GradingResult(
                answer_id=fixture.subjective_answer_id,
                submission_id=fixture.submission_id,
                question_type=QuestionType.SHORT_ANSWER,
                score=Decimal("6.00"),
                max_score=Decimal("10.00"),
                reason="说明了变量的作用。",
                correct_points=["保存数据"],
                missing_knowledge_points=["引用数据"],
                knowledge_points=["变量"],
                suggestions=["补充变量引用。"],
                retrieved_context_ids=[],
                confidence=0.3,
                validation_status=ValidationStatus.VALIDATED,
                review_status=ReviewStatus.PENDING_REVIEW,
            )
            session.add(grading)
            session.flush()
        record = ReviewRecord(
            grading_result_id=grading.id,
            reviewer_id=reviewer_id,
            decision=ReviewStatus.CONFIRMED,
            original_score=Decimal("6.00"),
            original_reason="说明了变量的作用。",
            original_knowledge_points=["变量"],
            final_score=Decimal("6.00"),
            final_reason="教师确认。",
            final_knowledge_points=["变量"],
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


# ---------------------------------------------------------------- P1.3 诊断顺序


class _FailingDiagnosisService:
    """P1.3 诊断失败替身：调用即抛出给定异常（不落库、不返回 Ready）。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[Any] = []

    async def generate(self, exam_result: Any) -> Any:
        self.calls.append(exam_result)
        raise self.error


class _ReadyDiagnosisService:
    """P1.3 就绪诊断替身：按生产契约带上应用层 ``exam_result_id``，供真实存储落库。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def generate(self, exam_result: Any) -> DiagnosisReportDTO:
        self.calls.append(exam_result)
        return DiagnosisReportDTO(
            exam_result_id=f"exam-result:{exam_result.submission_id}",
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=FIXED_NOW,
            source_exam_result_updated_at=exam_result.aggregated_at,
        )


def _diagnosis_adapter(env: dict[str, Any], service: Any) -> Any:
    """按生产顺序装配诊断：M4 服务 → DiagnosisRecorderAdapter → 真实诊断存储（P1.3）。"""

    from backend.app.api.workflow import DiagnosisRecorderAdapter
    from backend.app.services.grading.diagnosis_report_store import DiagnosisReportStore

    return DiagnosisRecorderAdapter(
        store=DiagnosisReportStore(session_factory=lambda: Session(env["engine"])),
        service=service,
    )


def _diagnosis_rows(env: dict[str, Any]) -> list[Any]:
    """读取该答卷在诊断表里的行（真实存储写入的事实）。"""

    from backend.app.models import DiagnosisReport

    with Session(env["engine"]) as session:
        return list(
            session.scalars(
                select(DiagnosisReport).where(
                    DiagnosisReport.submission_id == env["fixture"].submission_id
                )
            )
        )


def _accepted_service(env: dict[str, Any], *, diagnosis_service: Any) -> WorkflowService:
    """高置信度全部接受的启动入口（整卷结果为最终成绩）。"""

    return _service(
        env,
        agent=_AcceptedSubjectiveAgent(submission_id=str(env["fixture"].submission_id)),
        diagnosis_service=diagnosis_service,
    )










# ---------------------------------------------------------------- P1.4.1 启动收敛
