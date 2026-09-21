"""T077 复核 API 契约测试：权威查询源、字段白名单、部分成功与课程隔离。

TCR（2026-09-18，T077 / 评审 B03、B08～B11、遗漏5）：

- **既有覆盖缺口**：T074 已用替身验证了复核服务的写入与恢复语义，但没有任何真实责任方
  把“队列/详情读模型 → 教师决策提交 → 部分成功回执”串起来；也没有测试证明修订结果只能改
  分数与理由、重复提交不产生第二条 ``ReviewRecord``、检索依据按课程二次校验、跨课程不可见。
- **新增用例**：三端点权限边界、队列默认待复核与稳定分页、队列课程隔离与分页校验、
  详情权威字段（学生答案原文/分数/理由/知识点/参考答案/评分标准/检索依据/复核轨迹/运行身份）、
  检索依据课程过滤计数、确认与修改的决策载荷（含修订字段白名单）、陈旧决策 409、
  跨工作流 409、缺运行 409、修改缺分数或理由 422、超满分 422、重复提交幂等（无第二条记录）、
  部分成功回执（决定已保存、恢复未完成）、路由已注册。
- **对外契约**：plan §5/§5.2、FR-036/FR-037、T054 汇总、T060 结果存储、T062 ``ReviewRecord``、
  T064 ``WorkflowRun``、T073 检查点身份、T074 ``TeacherReviewDecision``/``ReviewOutcome``。

本文件只断言对外契约，不复制服务内部实现。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backend.app.api.reviews import (
    REVIEW_DECISION_REVISION_REQUIRED,
    REVIEW_DECISION_STALE,
    REVIEW_DECISION_WORKFLOW_MISMATCH,
    REVIEW_DECISION_WORKFLOW_REQUIRED,
    REVIEW_QUERY_ANSWER_NOT_FOUND,
    REVIEW_QUERY_PERMISSION_DENIED,
    ReviewDecisionService,
    ReviewQueryService,
    get_review_decision_service,
    get_review_query_service,
)
from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    DocumentStatus,
    GradingStatus,
    QuestionType,
    ReviewStatus,
    UserRole,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    GradingResult,
    KnowledgeBase,
    ReviewRecord,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.schemas.grading import ConfidenceDecisionDTO
from backend.app.services.auth_service import create_access_token
from backend.app.services.grading.grading_repository import CHECKPOINT_KIND
from backend.app.services.review_service import ReviewOutcome
from backend.app.services.workflow_checkpoint import WorkflowCheckpointStore
from tests.unit.models.sqlite_support import seed_submission
from tests.unit.services.test_submission_service import add_user
from tests.unit.settings_helpers import build_test_settings

WORKFLOW_ID = "grading-review-1"
REQUEST_ID = "request-review-1"
THREAD_ID = "thread-review-1"
FIXED_NOW = datetime(2026, 9, 18, 11, 0, tzinfo=UTC)


class _StubReviewService:
    """T074 复核服务替身：记录教师决策载荷并返回预置回执。"""

    def __init__(self, outcome: ReviewOutcome | None = None) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def submit_decision_async(
        self,
        decision: Any,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        comment: str | None = None,
    ) -> ReviewOutcome:
        self.calls.append(
            {
                "decision": decision,
                "actor_id": actor_id,
                "actor_role": actor_role,
                "comment": comment,
            }
        )
        if self.outcome is not None:
            return self.outcome
        return ReviewOutcome(
            workflow_id=decision.workflow_id,
            submission_id=str(self._submission_id),
            answer_id=decision.answer_id,
            decision=ReviewStatus(decision.review_status),
            review_record_id="record-1",
            workflow_status=WorkflowStatus.COMPLETED,
            interrupted=False,
            pending_answer_ids=(),
            resumable=False,
            resumed=True,
            resume_error_code=None,
            exam_result=None,
            exam_result_persisted=True,
            diagnosis=None,
            diagnosis_error_code=None,
        )

    _submission_id: Any = ""


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
    """隔离数据库 + 答卷夹具 + 一条待复核评分结果 + 一条已暂停运行。"""

    engine = _file_engine(tmp_path / "reviews.db")
    with Session(engine) as session:
        fixture = seed_submission(session)
        chunk = _seed_chunk(
            session,
            course_id=fixture.course_id,
            teacher_id=fixture.teacher_id,
            content="变量用于保存数据，并可在后续语句中引用。",
        )
        other_chunk = _seed_chunk(
            session,
            course_id=fixture.course_id,
            teacher_id=fixture.teacher_id,
            content="作用域决定变量的可见范围。",
            filename="作用域资料.pdf",
        )
        foreign_chunk = _seed_chunk(
            session,
            course_id=fixture.course_id,
            teacher_id=fixture.teacher_id,
            content="其他课程的片段应被过滤。",
            filename="其他课程.pdf",
            force_other_course=True,
        )
        grading = _seed_grading_result(
            session,
            fixture=fixture,
            chunk_ids=[str(chunk.id), str(foreign_chunk.id), "not-a-uuid"],
        )
        _seed_workflow_run(session, fixture=fixture)
        yield {
            "engine": engine,
            "fixture": fixture,
            "grading": grading,
            "own_chunk": chunk,
            "other_chunk": other_chunk,
            "foreign_chunk": foreign_chunk,
        }
    engine.dispose()


def _seed_chunk(
    session: Session,
    *,
    course_id: Any,
    teacher_id: Any,
    content: str,
    filename: str = "课程资料.pdf",
    force_other_course: bool = False,
) -> DocumentChunk:
    """写入一条真实课程片段；``force_other_course`` 用于构造跨课程引用。"""

    knowledge_base = KnowledgeBase(course_id=course_id, name=f"知识库 {filename}")
    session.add(knowledge_base)
    session.flush()
    if force_other_course:
        other_course = Course(name=f"其他课程 {filename}", created_by=teacher_id)
        session.add(other_course)
        session.flush()
        other = KnowledgeBase(course_id=other_course.id, name=f"其他知识库 {filename}")
        session.add(other)
        session.flush()
        knowledge_base = other
    document = Document(
        course_id=knowledge_base.course_id,
        knowledge_base_id=knowledge_base.id,
        uploaded_by=teacher_id,
        original_filename=filename,
        file_format="pdf",
        status=DocumentStatus.READY,
    )
    session.add(document)
    session.flush()
    chunk = DocumentChunk(
        document_id=document.id,
        course_id=knowledge_base.course_id,
        knowledge_base_id=knowledge_base.id,
        chunk_index=0,
        content=content,
        chunk_metadata={},
    )
    session.add(chunk)
    session.commit()
    return chunk


def _seed_grading_result(
    session: Session,
    *,
    fixture: Any,
    chunk_ids: list[str],
) -> GradingResult:
    """写入一条待人工复核的单题评分结果。"""

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
        retrieved_context_ids=chunk_ids,
        confidence=0.3,
        validation_status=ValidationStatus.VALIDATED,
        review_status=ReviewStatus.PENDING_REVIEW,
        decision_confidence=0.3,
        decision_threshold=0.8,
        decision_requires_review=True,
        decision_review_status=ReviewStatus.PENDING_REVIEW,
        decision_grading_status=GradingStatus.PENDING_REVIEW,
        decision_reason="置信度低于阈值，已进入人工复核队列。",
    )
    session.add(grading)
    session.commit()
    session.refresh(grading)
    return grading


def _seed_workflow_run(session: Session, *, fixture: Any) -> None:
    """写入一条暂停在人工复核的运行记录（含线程绑定）。"""

    store = WorkflowCheckpointStore(session=session, clock=lambda: FIXED_NOW)
    answer_id = str(fixture.subjective_answer_id)
    state: dict[str, Any] = {
        "workflow_id": WORKFLOW_ID,
        "request_id": REQUEST_ID,
        "submission_id": str(fixture.submission_id),
        "status": WorkflowStatus.PAUSED,
        "current_node": "Pending Review",
        "current_answer_id": answer_id,
        "current_answer_order": 2,
        "retry_count": 0,
        "pause_reason": "第 2 题评分结果需要教师复核，工作流已暂停。",
        "resumable": True,
        "review_status": ReviewStatus.PENDING_REVIEW,
        "confidence_decisions": {
            answer_id: ConfidenceDecisionDTO(
                confidence=0.3,
                threshold=0.8,
                requires_review=True,
                review_status=ReviewStatus.PENDING_REVIEW.value,
                grading_status="Pending Review",
                reason="置信度低于阈值，已进入人工复核队列。",
            )
        },
    }
    store.save_checkpoint(
        WORKFLOW_ID, state, "Pending Review", state["pause_reason"], thread_id=THREAD_ID
    )


def _seed_review_record(
    session: Session,
    *,
    fixture: Any,
    grading_id: Any,
    decision: ReviewStatus,
) -> ReviewRecord:
    """写入一条已落库的教师结论。"""

    record = ReviewRecord(
        grading_result_id=grading_id,
        reviewer_id=fixture.teacher_id,
        decision=decision,
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


def _users(env: dict[str, Any]) -> dict[str, User]:
    """读取夹具中的教师与学生账号。"""

    with Session(env["engine"]) as session:
        teacher = session.get(User, env["fixture"].teacher_id)
        student = session.get(User, env["fixture"].student_id)
        assert teacher is not None and student is not None
        return {"teacher": teacher, "student": student}


def _add_user(env: dict[str, Any], *, username: str, role: UserRole) -> User:
    """新增带指定角色的账号（跨课程/无权限用例）。"""

    with Session(env["engine"]) as session:
        return add_user(
            session, role, username=username, email=f"{username}@example.com"
        )


def _headers(user: User, role: UserRole) -> dict[str, str]:
    """签发指定角色的访问令牌。"""

    token = create_access_token(
        user.id,
        secret_key=build_test_settings().JWT_SECRET_KEY,
        roles=[role],
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}


def _query_service(env: dict[str, Any]) -> ReviewQueryService:
    """构造真实只读复核读模型。"""

    return ReviewQueryService(session_factory=lambda: Session(env["engine"]))


def _decision_service(
    env: dict[str, Any],
    stub: _StubReviewService,
) -> ReviewDecisionService:
    """构造决策服务：真实读模型 + 替身复核服务。"""

    stub._submission_id = env["fixture"].submission_id
    return ReviewDecisionService(
        query=_query_service(env),
        review_service=stub,
    )


@pytest.fixture
def client_factory(env: dict[str, Any]) -> Generator[Any, None, None]:
    """装配可注入复核服务的 TestClient 工厂。"""

    with ExitStack() as stack:

        def factory(
            query: ReviewQueryService | None = None,
            decision: ReviewDecisionService | None = None,
        ) -> TestClient:
            with gr.Blocks() as ui:
                gr.Markdown("复核契约测试")
            app = create_app(settings=build_test_settings(), gradio_app=ui)

            def database() -> Generator[Session, None, None]:
                with Session(env["engine"]) as session:
                    yield session

            app.dependency_overrides[get_db] = database
            app.dependency_overrides[get_review_query_service] = (
                lambda: query if query is not None else _query_service(env)
            )
            app.dependency_overrides[get_review_decision_service] = (
                lambda: decision
                if decision is not None
                else _decision_service(env, _StubReviewService())
            )
            return stack.enter_context(TestClient(app))

        yield factory


def _detail_url(env: dict[str, Any]) -> str:
    """详情端点路径。"""

    fixture = env["fixture"]
    return (
        f"/api/reviews/queue/{fixture.submission_id}/answers/"
        f"{fixture.subjective_answer_id}"
    )


def _decision_body(env: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """决策请求体。"""

    fixture = env["fixture"]
    body: dict[str, Any] = {
        "submission_id": str(fixture.submission_id),
        "answer_id": str(fixture.subjective_answer_id),
        "action": "confirm",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- 权限与路由


def test_endpoints_require_teacher_permission(
    env: dict[str, Any], client_factory: Any
) -> None:
    """未认证 401；学生与管理员没有复核权限，返回 403。"""

    client = client_factory()
    users = _users(env)
    admin = _add_user(env, username="admin-review", role=UserRole.ADMIN)

    assert client.get("/api/reviews/queue").status_code == 401
    assert client.get(_detail_url(env)).status_code == 401
    assert client.post("/api/reviews/decisions", json={}).status_code == 401

    for user, role in (
        (users["student"], UserRole.STUDENT),
        (admin, UserRole.ADMIN),
    ):
        headers = _headers(user, role)
        assert client.get("/api/reviews/queue", headers=headers).status_code == 403, role
        assert client.get(_detail_url(env), headers=headers).status_code == 403, role
        assert (
            client.post(
                "/api/reviews/decisions", headers=headers, json=_decision_body(env)
            ).status_code
            == 403
        ), role


def test_routes_registered(client_factory: Any) -> None:
    """端点在应用装配后可用。"""

    client = client_factory()
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/reviews/queue" in paths
    assert "/api/reviews/queue/{submission_id}/answers/{answer_id}" in paths
    assert "/api/reviews/decisions" in paths


# ---------------------------------------------------------------- 队列


def test_queue_defaults_to_pending_review(env: dict[str, Any], client_factory: Any) -> None:
    """队列默认只列出待人工复核，并回显分页事实。"""

    client = client_factory()
    response = client.get(
        "/api/reviews/queue", headers=_headers(_users(env)["teacher"], UserRole.TEACHER)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    item = body["items"][0]
    assert item["review_status"] == ReviewStatus.PENDING_REVIEW.value
    assert item["score"] == "6.00"
    assert item["confidence"] == 0.3
    assert item["question_number"] >= 1
    assert item["exam_title"]
    assert item["student_name"]


def test_queue_isolated_between_teachers(env: dict[str, Any], client_factory: Any) -> None:
    """队列按教师自己的课程范围收敛：其他教师看不到任何记录（空页，不泄露）。"""

    client = client_factory()
    other = _add_user(env, username="teacher-other", role=UserRole.TEACHER)

    response = client.get(
        "/api/reviews/queue", headers=_headers(other, UserRole.TEACHER)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_queue_rejects_invalid_page(env: dict[str, Any], client_factory: Any) -> None:
    """分页参数非法返回 422。"""

    client = client_factory()
    response = client.get(
        "/api/reviews/queue",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        params={"limit": 0},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "REVIEW_QUERY_INVALID_PAGE"


def test_queue_filters_by_review_status(env: dict[str, Any], client_factory: Any) -> None:
    """按状态过滤：已确认状态下队列为空（事实驱动，不猜测）。"""

    client = client_factory()
    response = client.get(
        "/api/reviews/queue",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        params={"review_status": ReviewStatus.CONFIRMED.value},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 0


# ---------------------------------------------------------------- 详情


def test_detail_returns_authoritative_facts(env: dict[str, Any], client_factory: Any) -> None:
    """详情返回学生答案原文、AI 评分、题目依据、复核轨迹与运行身份。"""

    client = client_factory()
    response = client.get(
        _detail_url(env), headers=_headers(_users(env)["teacher"], UserRole.TEACHER)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["student_answer"] == "变量用于保存数据。"
    assert body["score"] == "6.00"
    assert body["reason"] == "说明了变量的作用。"
    assert body["knowledge_points"] == ["变量"]
    assert body["missing_knowledge_points"] == ["引用数据"]
    assert body["reference_answer"]
    assert body["scoring_rubric"]
    assert body["confidence"] == 0.3
    assert body["review_status"] == ReviewStatus.PENDING_REVIEW.value
    assert body["workflow_id"] == WORKFLOW_ID
    assert body["thread_id"] == THREAD_ID
    assert body["workflow_status"] == WorkflowStatus.PAUSED.value
    assert body["review_records"] == []
    with Session(env["engine"]) as session:
        from backend.app.models import Question

        question = session.get(Question, env["fixture"].subjective_question_id)
        assert question is not None
        assert Decimal(body["max_score"]) == question.score


def test_detail_ignores_background_task_run_for_same_submission(
    env: dict[str, Any], client_factory: Any
) -> None:
    """复核详情只认 M4 运行身份：更新的 M3 后台任务行不得顶替线程与工作流状态（H05）。"""

    with Session(env["engine"]) as session:
        session.add(
            WorkflowRun(
                workflow_id="background-task-1",
                request_id="background-request",
                submission_id=env["fixture"].submission_id,
                status=WorkflowStatus.COMPLETED,
                checkpoint={
                    "kind": CHECKPOINT_KIND,
                    "task": {},
                    "current_node": "aggregate",
                },
                current_node="aggregate",
            )
        )
        session.commit()
    client = client_factory()

    response = client.get(
        _detail_url(env), headers=_headers(_users(env)["teacher"], UserRole.TEACHER)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_id"] == WORKFLOW_ID
    assert body["thread_id"] == THREAD_ID
    assert body["workflow_status"] == WorkflowStatus.PAUSED.value


def test_detail_evidence_is_course_scoped(env: dict[str, Any], client_factory: Any) -> None:
    """检索依据按课程二次校验：本课程片段返回正文，其他课程与不可解析引用只计数。"""

    client = client_factory()
    body = client.get(
        _detail_url(env), headers=_headers(_users(env)["teacher"], UserRole.TEACHER)
    ).json()

    assert [item["chunk_id"] for item in body["evidence"]] == [str(env["own_chunk"].id)]
    assert body["evidence"][0]["content"].startswith("变量用于保存数据")
    assert body["evidence"][0]["source_file"] == "课程资料.pdf"
    assert body["out_of_course_evidence_count"] == 1
    assert body["unresolved_evidence_count"] == 1


def test_detail_isolated_between_teachers(env: dict[str, Any], client_factory: Any) -> None:
    """跨课程读取详情返回 403，不泄露其他课程事实。"""

    client = client_factory()
    other = _add_user(env, username="teacher-outsider", role=UserRole.TEACHER)

    response = client.get(_detail_url(env), headers=_headers(other, UserRole.TEACHER))

    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == REVIEW_QUERY_PERMISSION_DENIED


def test_detail_missing_grading_result_returns_404(
    env: dict[str, Any], client_factory: Any
) -> None:
    """没有评分结果的答案返回 404。"""

    client = client_factory()
    fixture = env["fixture"]
    url = (
        f"/api/reviews/queue/{fixture.submission_id}/answers/"
        f"{fixture.objective_answer_id}"
    )

    response = client.get(
        url, headers=_headers(_users(env)["teacher"], UserRole.TEACHER)
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == REVIEW_QUERY_ANSWER_NOT_FOUND


# ---------------------------------------------------------------- 决策


def test_confirm_decision_delegates_with_authoritative_identity(
    env: dict[str, Any], client_factory: Any
) -> None:
    """确认决策按数据库身份委托 T074，并返回已完成回执。"""

    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env, comment="核对无误。"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == ReviewStatus.CONFIRMED.value
    assert body["decision_saved"] is True
    assert body["resume_status"] == "succeeded"
    assert body["review_record_id"] == "record-1"
    assert stub.calls
    decision = stub.calls[0]["decision"]
    assert decision.workflow_id == WORKFLOW_ID
    assert decision.thread_id == THREAD_ID
    assert decision.answer_id == str(env["fixture"].subjective_answer_id)
    assert decision.review_status == ReviewStatus.CONFIRMED.value
    assert decision.revised_result is None
    assert decision.expected_review_status == ReviewStatus.PENDING_REVIEW.value
    assert stub.calls[0]["comment"] == "核对无误。"
    assert stub.calls[0]["actor_role"] == UserRole.TEACHER


def test_modify_decision_only_accepts_score_and_reason(
    env: dict[str, Any], client_factory: Any
) -> None:
    """修改决策：只替换分数与理由，其余字段取数据库权威原结果（字段白名单）。"""

    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env, action="modify", score="8.50", reason="补充引用说明。"),
    )

    assert response.status_code == 200
    decision = stub.calls[0]["decision"]
    revised = decision.revised_result
    assert decision.review_status == ReviewStatus.MODIFIED.value
    assert revised is not None
    assert revised.score == Decimal("8.50")
    assert revised.reason == "补充引用说明。"
    assert revised.answer_id == str(env["fixture"].subjective_answer_id)
    assert revised.submission_id == str(env["fixture"].submission_id)
    assert revised.question_type is QuestionType.SHORT_ANSWER
    assert revised.max_score == Decimal("10.00")
    assert revised.knowledge_points == ["变量"]
    assert revised.confidence == 0.3


def test_modify_requires_score_and_reason(env: dict[str, Any], client_factory: Any) -> None:
    """修改决策缺少分数或理由返回 422，且不调用复核服务。"""

    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))
    headers = _headers(_users(env)["teacher"], UserRole.TEACHER)

    missing_reason = client.post(
        "/api/reviews/decisions",
        headers=headers,
        json=_decision_body(env, action="modify", score="8.00"),
    )
    assert missing_reason.status_code == 422
    assert (
        missing_reason.json()["detail"]["error_code"] == REVIEW_DECISION_REVISION_REQUIRED
    )

    missing_score = client.post(
        "/api/reviews/decisions",
        headers=headers,
        json=_decision_body(env, action="modify", reason="理由。"),
    )
    assert missing_score.status_code == 422
    assert stub.calls == []


def test_modify_rejects_score_above_max(env: dict[str, Any], client_factory: Any) -> None:
    """修订分数超出满分返回 422。"""

    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env, action="modify", score="99.00", reason="理由。"),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "REVIEW_DECISION_INVALID_SCORE"
    assert stub.calls == []


def test_unsupported_action_is_rejected(env: dict[str, Any], client_factory: Any) -> None:
    """本批不支持的决策类型（重评/最终）由请求校验与端点共同明确拒绝。"""

    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env, action="regrade"),
    )

    assert response.status_code == 422
    assert stub.calls == []


def test_decision_rejects_stale_state(env: dict[str, Any], client_factory: Any) -> None:
    """该题已形成教师结论时再次提交返回 409，且不调用复核服务。"""

    with Session(env["engine"]) as session:
        grading = session.get(GradingResult, env["grading"].id)
        assert grading is not None
        grading.review_status = ReviewStatus.CONFIRMED
        session.commit()
    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == REVIEW_DECISION_STALE
    assert stub.calls == []


def test_duplicate_decision_is_idempotent(env: dict[str, Any], client_factory: Any) -> None:
    """同一结论重复提交按既有记录幂等返回，不写第二条 ReviewRecord。"""

    with Session(env["engine"]) as session:
        record = _seed_review_record(
            session,
            fixture=env["fixture"],
            grading_id=env["grading"].id,
            decision=ReviewStatus.CONFIRMED,
        )
        record_id = str(record.id)
        before = len(
            list(
                session.scalars(
                    select(ReviewRecord).where(
                        ReviewRecord.grading_result_id == env["grading"].id
                    )
                )
            )
        )
    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision_saved"] is True
    assert body["review_record_id"] == record_id
    assert body["resume_status"] == "pending"
    assert "未重复写入" in body["message"]
    assert stub.calls == []
    with Session(env["engine"]) as session:
        after = len(
            list(
                session.scalars(
                    select(ReviewRecord).where(
                        ReviewRecord.grading_result_id == env["grading"].id
                    )
                )
            )
        )
    assert after == before == 1


def test_decision_reports_partial_success(env: dict[str, Any], client_factory: Any) -> None:
    """决定已保存但恢复失败时返回部分成功回执，不伪装成已恢复。"""

    outcome = ReviewOutcome(
        workflow_id=WORKFLOW_ID,
        submission_id=str(env["fixture"].submission_id),
        answer_id=str(env["fixture"].subjective_answer_id),
        decision=ReviewStatus.MODIFIED,
        review_record_id="record-9",
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
    stub = _StubReviewService(outcome=outcome)
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env, action="modify", score="7.00", reason="理由更新。"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision_saved"] is True
    assert body["resume_status"] == "failed"
    assert body["resume_error_code"] == "REVIEW_SERVICE_CONFLICT"
    assert body["review_record_id"] == "record-9"
    assert body["pending_review_count"] == 1
    assert "恢复未完成" in body["message"]


def test_decision_rejects_workflow_mismatch(env: dict[str, Any], client_factory: Any) -> None:
    """客户端声明的运行与数据库事实不一致时返回 409，拒绝跨工作流提交。"""

    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env, workflow_id="grading-other"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == REVIEW_DECISION_WORKFLOW_MISMATCH
    assert stub.calls == []


def test_decision_requires_workflow_run(env: dict[str, Any], client_factory: Any) -> None:
    """答卷没有运行记录时返回 409，不凭空创建复核结论。"""

    with Session(env["engine"]) as session:
        run = session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == WORKFLOW_ID)
        ).one()
        session.delete(run)
        session.commit()
    stub = _StubReviewService()
    client = client_factory(decision=_decision_service(env, stub))

    response = client.post(
        "/api/reviews/decisions",
        headers=_headers(_users(env)["teacher"], UserRole.TEACHER),
        json=_decision_body(env),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == REVIEW_DECISION_WORKFLOW_REQUIRED
    assert stub.calls == []


def test_decision_records_review_records_in_detail(
    env: dict[str, Any], client_factory: Any
) -> None:
    """已落库的教师结论在详情中作为审计轨迹返回。"""

    with Session(env["engine"]) as session:
        _seed_review_record(
            session,
            fixture=env["fixture"],
            grading_id=env["grading"].id,
            decision=ReviewStatus.CONFIRMED,
        )
    client = client_factory()

    body = client.get(
        _detail_url(env), headers=_headers(_users(env)["teacher"], UserRole.TEACHER)
    ).json()

    assert len(body["review_records"]) == 1
    record = body["review_records"][0]
    assert record["decision"] == ReviewStatus.CONFIRMED.value
    assert record["original_score"] == "6.00"
    assert record["final_score"] == "6.00"
    assert record["reviewer_id"] == str(env["fixture"].teacher_id)


def test_submission_status_is_untouched_by_query(env: dict[str, Any], client_factory: Any) -> None:
    """只读查询不得改变答卷生命周期状态。"""

    client = client_factory()
    client.get("/api/reviews/queue", headers=_headers(_users(env)["teacher"], UserRole.TEACHER))
    client.get(_detail_url(env), headers=_headers(_users(env)["teacher"], UserRole.TEACHER))

    with Session(env["engine"]) as session:
        submission = session.get(Submission, env["fixture"].submission_id)
        assert submission is not None
        assert submission.status.value == "Submitted"
