"""T076 Workflow API 契约测试：启动幂等、角色差异、唯一恢复入口与未就绪 503。

TCR（2026-09-18，T076 / 评审 B03～B07、B11、遗漏2、遗漏3）：

- **既有覆盖缺口**：T072/T073/T074 已各自验证图、持久检查点与复核服务，但没有真实责任方把
  “启动运行 → 落运行记录 → 状态查询 → 恢复”串起来；也没有测试证明重复启动不产生第二个活动运行、
  学生看不到内部状态、人工复核暂停不能用裸恢复绕过教师结论、依赖缺失时在产生副作用之前返回 503。
- **新增用例**：权限边界（未认证/学生/管理员/跨课程教师）、启动正常路径（真实
  ``WorkflowService`` + 真实 ``DatabaseCheckpointSaver`` + 真实 T060 快照读取，外部 Agent/诊断用替身）、
  重复启动幂等复用、非法答卷状态 409、已完成运行 409 与显式重评拒绝、教师/学生状态字段差异、
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

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backend.app.ai.agents.invocation import AgentInvocation
from backend.app.ai.agents.state import AgentOutput, AgentStatus, AgentType
from backend.app.ai.workflows.grading_handoff import PENDING_REVIEW
from backend.app.api.workflow import (
    WORKFLOW_REVIEW_DECISION_REQUIRED,
    WORKFLOW_RUN_ALREADY_COMPLETED,
    WORKFLOW_RUN_NOT_RESUMABLE,
    WORKFLOW_RUN_REGRADE_UNSUPPORTED,
    WORKFLOW_SERVICE_NOT_READY,
    WORKFLOW_SUBMISSION_NOT_READY,
    WorkflowService,
    get_workflow_service,
)
from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    AnswerStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    UserRole,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import (
    Answer,
    GradingResult,
    ReviewRecord,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.schemas.ai import GradingResult as GradingResultPayload
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultStatus,
)
from backend.app.services.auth_service import create_access_token
from backend.app.services.grading.grading_repository import (
    CHECKPOINT_KIND,
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
        del snapshot, session, settings
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

        def factory(service: WorkflowService | None = None) -> TestClient:
            with gr.Blocks() as ui:
                gr.Markdown("工作流契约测试")
            app = create_app(settings=build_test_settings(), gradio_app=ui)

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


def test_endpoints_require_authentication(env: dict[str, Any], client_factory: Any) -> None:
    """三个端点未认证时一律 401（认证中间件）。"""

    client = client_factory(_service(env))
    submission_id = str(env["fixture"].submission_id)

    assert (
        client.post(f"/api/workflow/submissions/{submission_id}/runs", json={}).status_code
        == 401
    )
    assert client.get("/api/workflow/runs/whatever").status_code == 401
    assert client.post("/api/workflow/runs/whatever/resume", json={}).status_code == 401


def test_start_and_resume_reject_non_teacher(
    env: dict[str, Any], client_factory: Any
) -> None:
    """学生与管理员没有触发权限，启动与恢复返回 403。"""

    client = client_factory(_service(env))
    submission_id = str(env["fixture"].submission_id)
    users = _users(env)
    admin = _add_user(env, username="admin-1", role=UserRole.ADMIN)

    for user, role in ((users["student"], UserRole.STUDENT), (admin, UserRole.ADMIN)):
        response = client.post(
            f"/api/workflow/submissions/{submission_id}/runs",
            headers=_headers(user, role),
            json={"regrade": False},
        )
        assert response.status_code == 403
        resumed = client.post(
            "/api/workflow/runs/unknown/resume",
            headers=_headers(user, role),
            json={},
        )
        assert resumed.status_code == 403


def test_routes_registered(client_factory: Any) -> None:
    """端点在应用装配后可用（避免模块写完但应用未加载）。"""

    client = client_factory()
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/workflow/submissions/{submission_id}/runs" in paths
    assert "/api/workflow/runs/{workflow_id}" in paths
    assert "/api/workflow/runs/{workflow_id}/resume" in paths


def test_start_requires_course_ownership(env: dict[str, Any], client_factory: Any) -> None:
    """不属于自己课程的答卷返回 403，且不产生运行记录。"""

    client = client_factory(_service(env))
    other = _add_user(env, username="teacher-other", role=UserRole.TEACHER)
    submission_id = str(env["fixture"].submission_id)

    response = client.post(
        f"/api/workflow/submissions/{submission_id}/runs",
        headers=_headers(other, UserRole.TEACHER),
        json={"regrade": False},
    )

    assert response.status_code == 403
    assert _runs(env) == []


# ---------------------------------------------------------------- 启动


def test_start_run_pauses_for_review_with_persistent_checkpoint(
    env: dict[str, Any], client_factory: Any
) -> None:
    """真实服务 + 真实持久 Checkpointer 跑到 interrupt：状态可查、待复核答案可见。"""

    client = client_factory(_service(env))
    response = _start(client, env)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == WorkflowStatus.PAUSED.value
    assert body["reused"] is False
    assert body["thread_id"]
    assert body["current_node"] == PENDING_REVIEW
    assert body["pause_reason"]
    assert body["resumable"] is True
    assert body["pending_answer_ids"] == [str(env["fixture"].subjective_answer_id)]

    runs = _runs(env)
    assert len(runs) == 1
    assert runs[0].workflow_id == body["workflow_id"]
    assert runs[0].submission_id == env["fixture"].submission_id
    assert runs[0].request_id == body["request_id"]

    detail = client.get(
        f"/api/workflow/runs/{body['workflow_id']}",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
    )
    assert detail.status_code == 200
    assert detail.json()["workflow_id"] == body["workflow_id"]
    assert detail.json()["pending_answer_ids"] == body["pending_answer_ids"]
    # 客户端响应不得包含 LangGraph runtime 检查点载荷。
    assert "checkpoint" not in detail.json()


def test_repeated_start_reuses_active_run(env: dict[str, Any], client_factory: Any) -> None:
    """重复启动复用同一运行，不并行创建第二个活动运行。"""

    client = client_factory(_service(env))
    first = _start(client, env).json()
    second = _start(client, env)

    assert second.status_code == 200
    body = second.json()
    assert body["workflow_id"] == first["workflow_id"]
    assert body["thread_id"] == first["thread_id"]
    assert body["reused"] is True
    assert len(_runs(env)) == 1


def test_start_ignores_background_task_run_for_same_submission(
    env: dict[str, Any], client_factory: Any
) -> None:
    """M3 后台任务行不参与 M4 幂等启动：同一答卷仍创建本执行器的运行并按其复用（H05）。"""

    with Session(env["engine"]) as session:
        session.add(
            WorkflowRun(
                workflow_id="background-task-1",
                request_id="background-request",
                submission_id=env["fixture"].submission_id,
                status=WorkflowStatus.PAUSED,
                checkpoint={
                    "kind": CHECKPOINT_KIND,
                    "task": {},
                    "current_node": "score",
                },
                pause_reason="存在待人工复核题目：本轮评分已完成，成绩尚未最终确认。",
                current_node="score",
            )
        )
        session.commit()
    client = client_factory(_service(env))

    response = _start(client, env)

    assert response.status_code == 200
    body = response.json()
    assert body["reused"] is False
    assert body["workflow_id"] != "background-task-1"
    runs = _runs(env)
    assert len(runs) == 2
    legacy = next(row for row in runs if row.workflow_id == "background-task-1")
    assert legacy.status is WorkflowStatus.PAUSED
    assert (legacy.checkpoint or {})["kind"] == CHECKPOINT_KIND

    repeated = _start(client, env)
    assert repeated.status_code == 200
    assert repeated.json()["reused"] is True
    assert repeated.json()["workflow_id"] == body["workflow_id"]
    assert len(_runs(env)) == 2


def test_start_persists_pending_review_outcome(
    env: dict[str, Any], client_factory: Any
) -> None:
    """低置信度暂停：单题结果与待复核整卷结果随正式启动落库，运行保持 Paused（H01）。"""

    client = client_factory(_service(env))
    body = _start(client, env).json()

    assert body["status"] == WorkflowStatus.PAUSED.value
    repository = _repository(env)
    stored = repository.get_exam_result(str(env["fixture"].submission_id))
    assert stored is not None
    assert stored.is_final is False
    assert stored.result_status == ExamResultStatus.PENDING_REVIEW
    assert stored.final_total_score is None
    # P1.2.5：题序确定——待复核整卷结果的题目数必须等于提交答案数，且题序 1 为客观题。
    assert [item.answer_id for item in stored.items] == [
        str(env["fixture"].objective_answer_id),
        str(env["fixture"].subjective_answer_id),
    ]
    assert [item.order for item in stored.items] == [1, 2]
    assert [item.question_type for item in stored.items] == [
        QuestionType.SINGLE_CHOICE,
        QuestionType.SHORT_ANSWER,
    ]
    single = repository.get_single_result(
        str(env["fixture"].submission_id), str(env["fixture"].subjective_answer_id)
    )
    assert single is not None
    assert single.score == Decimal("6.00")
    assert single.decision is not None
    assert single.decision.requires_review is True
    assert single.decision.threshold == 0.8
    with Session(env["engine"]) as session:
        run = session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == body["workflow_id"])
        ).one()
        assert run.status is WorkflowStatus.PAUSED
        assert run.pause_reason
        assert run.resumable is True
        assert run.exam_result_id is not None
        rows = {
            row.answer_id: row
            for row in session.scalars(select(GradingResult))
        }
        # P1.2.5 之后题序确定：暂停必停在题序 2，题序 1 的客观题必须已经落库（旧实现按 UUID
        # 排序时会把主观题排到题序 1，导致只落库一题）。
        assert set(rows) == {
            env["fixture"].objective_answer_id,
            env["fixture"].subjective_answer_id,
        }
        subjective = rows[env["fixture"].subjective_answer_id]
        assert subjective.review_status is ReviewStatus.PENDING_REVIEW
        assert subjective.decision_review_status is ReviewStatus.PENDING_REVIEW
        submission = session.get(Submission, env["fixture"].submission_id)
        assert submission is not None
        assert submission.status is SubmissionStatus.GRADED


def test_start_persists_final_result_for_accepted_run(
    env: dict[str, Any], client_factory: Any
) -> None:
    """高置信度全部接受：最终成绩与运行终态随正式启动落库（H01）。"""

    client = client_factory(
        _service(
            env,
            agent=_AcceptedSubjectiveAgent(
                submission_id=str(env["fixture"].submission_id)
            ),
        )
    )
    body = _start(client, env).json()

    assert body["status"] == WorkflowStatus.COMPLETED.value
    repository = _repository(env)
    stored = repository.get_exam_result(str(env["fixture"].submission_id))
    assert stored is not None
    assert stored.is_final is True
    assert stored.result_status == ExamResultStatus.FINAL
    assert stored.final_total_score == Decimal("16.00")
    with Session(env["engine"]) as session:
        run = session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == body["workflow_id"])
        ).one()
        assert run.status is WorkflowStatus.COMPLETED
        assert run.exam_result_id is not None
        assert all(
            answer.status is AnswerStatus.GRADED
            for answer in session.scalars(select(Answer))
        )
        submission = session.get(Submission, env["fixture"].submission_id)
        assert submission is not None
        assert submission.status is SubmissionStatus.GRADED
        assert submission.graded_at is not None


def test_start_rejects_draft_submission(env: dict[str, Any], client_factory: Any) -> None:
    """草稿答卷不得启动阅卷，返回 409 且不写运行记录。"""

    with Session(env["engine"]) as session:
        submission = session.get(Submission, env["fixture"].submission_id)
        assert submission is not None
        submission.status = SubmissionStatus.DRAFT
        session.commit()
    client = client_factory(_service(env))

    response = _start(client, env)

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == WORKFLOW_SUBMISSION_NOT_READY
    assert _runs(env) == []


def test_start_rejects_completed_run_and_explicit_regrade(
    env: dict[str, Any], client_factory: Any
) -> None:
    """已完成运行返回 409；显式重评本批不支持，返回明确的 409 错误码。"""

    client = client_factory(_service(env))
    workflow_id = _start(client, env).json()["workflow_id"]
    with Session(env["engine"]) as session:
        row = session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        ).one()
        row.status = WorkflowStatus.COMPLETED
        session.commit()

    completed = _start(client, env)
    assert completed.status_code == 409
    assert completed.json()["detail"]["error_code"] == WORKFLOW_RUN_ALREADY_COMPLETED

    regrade = _start(client, env, regrade=True)
    assert regrade.status_code == 409
    assert regrade.json()["detail"]["error_code"] == WORKFLOW_RUN_REGRADE_UNSUPPORTED


# ---------------------------------------------------------------- 状态查询角色差异


def test_student_sees_only_public_progress(env: dict[str, Any], client_factory: Any) -> None:
    """学生只能看到自己的公开进度，越权学生返回 403。"""

    client = client_factory(_service(env))
    workflow_id = _start(client, env).json()["workflow_id"]
    users = _users(env)
    outsider = _add_user(env, username="student-outside", role=UserRole.STUDENT)

    mine = client.get(
        f"/api/workflow/runs/{workflow_id}",
        headers=_headers(users["student"], UserRole.STUDENT),
    )
    assert mine.status_code == 200
    body = mine.json()
    assert body["status"] == WorkflowStatus.PAUSED.value
    assert body["requires_teacher_review"] is True
    assert body["pending_review_count"] == 1
    assert body["finished"] is False
    for internal in ("current_node", "pause_reason", "pending_answer_ids", "request_id"):
        assert internal not in body

    other = client.get(
        f"/api/workflow/runs/{workflow_id}",
        headers=_headers(outsider, UserRole.STUDENT),
    )
    assert other.status_code == 403


def test_unknown_run_returns_404(env: dict[str, Any], client_factory: Any) -> None:
    """不存在的运行返回 404。"""

    client = client_factory(_service(env))
    response = client.get(
        "/api/workflow/runs/grading-missing",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
    )

    assert response.status_code == 404


# ---------------------------------------------------------------- 恢复


def test_resume_requires_teacher_decision_for_review_pause(
    env: dict[str, Any], client_factory: Any
) -> None:
    """人工复核暂停：缺少目标答案或缺少教师结论都返回 409。"""

    client = client_factory(_service(env))
    workflow_id = _start(client, env).json()["workflow_id"]
    headers = _headers(_users(env)["teacher"], UserRole.TEACHER)
    answer_id = str(env["fixture"].subjective_answer_id)

    missing_answer = client.post(
        f"/api/workflow/runs/{workflow_id}/resume", headers=headers, json={}
    )
    assert missing_answer.status_code == 409
    assert (
        missing_answer.json()["detail"]["error_code"] == WORKFLOW_REVIEW_DECISION_REQUIRED
    )

    no_record = client.post(
        f"/api/workflow/runs/{workflow_id}/resume",
        headers=headers,
        json={"answer_id": answer_id},
    )
    assert no_record.status_code == 409
    assert no_record.json()["detail"]["error_code"] == WORKFLOW_REVIEW_DECISION_REQUIRED


def test_resume_delegates_to_review_service_and_reports_partial_success(
    env: dict[str, Any], client_factory: Any
) -> None:
    """已有教师结论时委托 T074 恢复；“已保存待继续”以部分成功回执表达。"""

    base_outcome = ReviewOutcome(
        workflow_id="pending",
        submission_id=str(env["fixture"].submission_id),
        answer_id=str(env["fixture"].subjective_answer_id),
        decision=ReviewStatus.CONFIRMED,
        review_record_id=None,
        workflow_status=WorkflowStatus.PAUSED,
        interrupted=True,
        pending_answer_ids=(str(env["fixture"].subjective_answer_id),),
        resumable=True,
        resumed=False,
        resume_error_code="REVIEW_SERVICE_CONFLICT",
        exam_result=None,
        exam_result_persisted=False,
        diagnosis=None,
        diagnosis_error_code=None,
    )
    stub_review = _StubReviewService(base_outcome)
    service = _service(env, review_service_provider=lambda: stub_review)
    client = client_factory(service)
    started = _start(client, env).json()
    workflow_id = started["workflow_id"]
    stub_review.outcome = replace(base_outcome, workflow_id=workflow_id)
    _record_decision(env, reviewer_id=env["fixture"].teacher_id)

    response = client.post(
        f"/api/workflow/runs/{workflow_id}/resume",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json={"answer_id": str(env["fixture"].subjective_answer_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision_saved"] is True
    assert body["resumed"] is False
    assert body["resume_error_code"] == "REVIEW_SERVICE_CONFLICT"
    assert "待继续" in body["message"] or "恢复未完成" in body["message"]
    assert stub_review.calls
    decision = stub_review.calls[0]["decision"]
    assert decision.workflow_id == workflow_id
    assert decision.review_status == ReviewStatus.CONFIRMED.value
    assert stub_review.calls[0]["actor_role"] == UserRole.TEACHER


def test_resume_without_runtime_checkpoint_is_rejected(
    env: dict[str, Any], client_factory: Any
) -> None:
    """没有持久 runtime 检查点的暂停运行不提供自动恢复。"""

    service = _service(env)
    client = client_factory(service)
    state = {
        "workflow_id": "grading-without-runtime",
        "request_id": "request-without-runtime",
        "submission_id": str(env["fixture"].submission_id),
        "status": WorkflowStatus.PAUSED,
        "current_node": PENDING_REVIEW,
        "pause_reason": "系统故障暂停。",
        "retry_count": 0,
        "confidence_decisions": {},
        "grading_results": {},
    }
    _store(env).save_checkpoint(
        "grading-without-runtime",
        state,
        PENDING_REVIEW,
        "系统故障暂停。",
        thread_id="thread-without-runtime",
    )

    response = client.post(
        "/api/workflow/runs/grading-without-runtime/resume",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json={},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == WORKFLOW_RUN_NOT_RESUMABLE


# ---------------------------------------------------------------- 未就绪


def test_unwired_service_returns_503_without_side_effects(
    env: dict[str, Any], client_factory: Any
) -> None:
    """依赖未接线时在产生任何副作用前返回 503，不创建运行记录。"""

    service = _service(env, agent=None, diagnosis_service=None)
    client = client_factory(service)

    response = _start(client, env)

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == WORKFLOW_SERVICE_NOT_READY
    assert _runs(env) == []


def test_readiness_failure_is_reported_before_side_effects(
    env: dict[str, Any], client_factory: Any
) -> None:
    """答卷读取未接线时同样 503，且不写运行记录。"""

    service = _service(env, reader=None)
    client = client_factory(service)

    response = _start(client, env)

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == WORKFLOW_SERVICE_NOT_READY
    assert _runs(env) == []


def test_production_checkpointer_is_persisted(env: dict[str, Any], client_factory: Any) -> None:
    """启动后运行记录里确实有持久 runtime 检查点（证明注入的是数据库 Checkpointer）。"""

    client = client_factory(_service(env))
    body = _start(client, env).json()
    runs = _runs(env)
    assert len(runs) == 1
    row = runs[0]

    assert row.resumable is True
    assert row.checkpoint is not None
    assert row.checkpoint.get("runtime") is not None
    assert str(row.checkpoint.get("thread_id")) == str(body["thread_id"])
    assert _store(env).runtime_ready(row.workflow_id, str(body["thread_id"])) is True


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
