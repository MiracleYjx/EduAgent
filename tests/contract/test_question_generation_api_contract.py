"""T075 AI 出题 API 契约测试：两阶段状态机、原子落库与对象级授权。

TCR（2026-09-18，T075 / 评审 B01～B03、B11、遗漏1）：

- **既有覆盖缺口**：T067/T068 各自用替身验证了 Agent 与校验器，但没有真实责任方把
  “生成 → 自动校验 → 候选题落库”串起来；没有测试证明候选题**整批原子写入**、字段保真、
  教师只能审核已持久化的 ``Pending Review``、以及跨课程不可见。
- **新增用例**：生成端整批落库（含 ``Pending Review`` 与 ``Needs Revision`` 两种结论）、
  字面字段保真、检索上下文不足 422 且零副作用、Provider 未就绪 503 且零副作用、
  写库中途失败整批回滚、候选列表课程隔离与分页、跨课程详情 404、教师审核状态机与陈旧断言、
  未认证/无权限/路由已注册。
- **对外契约**：plan §4.1（教师条件 → Query → 检索 → Question Agent → Schema → Validator →
  教师审核 → 题库）、FR-024～FR-028、T067/T068、T068 ``plan_transition`` 与
  ``CANDIDATE_STATUS_TRANSITIONS``。

本文件只断言对外契约，不复制服务内部实现。
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Sequence
from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.ai.agents.question_agent import (
    QUESTION_INSUFFICIENT_CONTEXT,
    QUESTION_PROVIDER_NOT_READY,
    ProviderNotReadyError,
    QuestionAgent,
)
from backend.app.api import question_generation as qg
from backend.app.api.question_generation import (
    QUESTION_CANDIDATE_NOT_PENDING_REVIEW,
    QUESTION_CANDIDATE_STALE,
    QUESTION_CANDIDATE_STORE_NOT_READY,
    QUESTION_REVISION_COMMENT_REQUIRED,
    CandidateStoreNotReadyError,
    QuestionGenerationService,
    get_question_generation_service,
)
from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import (
    DocumentStatus,
    QuestionStatus,
    QuestionType,
    UserRole,
)
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Question,
    User,
)
from backend.app.services.auth_service import create_access_token
from backend.app.services.course_service import CourseService
from backend.app.services.question_validator import QuestionValidator
from tests.support.question_generation_doubles import (
    StubEmbeddingProvider,
    StubQuestionProvider,
    StubRetriever,
    make_candidate,
    make_chunk,
)
from tests.unit.services.test_submission_service import (
    add_course,
    add_student,
    add_teacher,
    add_user,
)
from tests.unit.settings_helpers import build_test_settings

#: 单题无法使用的评分标准：只含分值占位，T068 判定为 Needs Revision。
UNUSABLE_RUBRIC = "2 分"


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """为契约测试创建隔离数据库。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


@pytest.fixture
def scenario(session: Session) -> dict[str, Any]:
    """创建两名教师、各自课程以及学生与管理员账号。"""

    teacher = add_teacher(session)
    other_teacher = add_user(
        session, UserRole.TEACHER, username="teacher-2", email="teacher2@example.com"
    )
    course = add_course(session, teacher)
    other_course_summary = CourseService(session).create_course(
        name="Python 进阶", created_by=other_teacher.id
    )
    other_course = session.get(Course, UUID(other_course_summary.id))
    assert other_course is not None
    return {
        "session": session,
        "teacher": teacher,
        "other_teacher": other_teacher,
        "course": course,
        "other_course": other_course,
        "student": add_student(session, username="student-1"),
        "admin": add_user(
            session, UserRole.ADMIN, username="admin", email="admin@example.com"
        ),
    }


class _FailingProvider:
    """按指定异常失败的出题 Provider 替身。"""

    provider_name = "failing-question"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def generate_structured(
        self,
        messages: Sequence[Any],
        schema: Any,
        model: str | None = None,
    ) -> Any:
        raise self.error


def _service(
    session: Session,
    *,
    candidates: Sequence[Any] | None = None,
    chunks: Sequence[Any] | None = None,
    provider_error: Exception | None = None,
) -> QuestionGenerationService:
    """构造注入替身 Provider/检索/Embedding 的真实出题服务。"""

    provider: Any = (
        _FailingProvider(provider_error)
        if provider_error is not None
        else StubQuestionProvider(candidates=candidates or (make_candidate(),))
    )
    return QuestionGenerationService(
        session=session,
        agent=QuestionAgent(provider=provider),
        validator=QuestionValidator(),
        retriever=StubRetriever(chunks if chunks is not None else [make_chunk("chunk-1")]),
        embedding_provider=StubEmbeddingProvider(),
        settings=build_test_settings(),
    )


def _generate(
    session: Session,
    *,
    course: Course,
    teacher: User,
    candidates: Sequence[Any],
    chunks: Sequence[Any] | None = None,
    provider_error: Exception | None = None,
) -> Any:
    """直接驱动服务完成一次生成，返回响应载荷。"""

    service = _service(
        session,
        candidates=candidates,
        chunks=chunks,
        provider_error=provider_error,
    )
    return asyncio.run(
        service.generate_candidates(
            course_id=str(course.id),
            actor_id=str(teacher.id),
            request_id="request-generate-1",
            count=len(candidates),
        )
    )


def _seed_chunk(
    session: Session,
    *,
    course: Course,
    teacher: User,
    content: str,
) -> Any:
    """写入一条真实的课程片段，供检索依据的课程校验用例使用。"""

    knowledge_base = KnowledgeBase(course_id=course.id, name=f"{course.name} 知识库")
    session.add(knowledge_base)
    session.flush()
    document = Document(
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        uploaded_by=teacher.id,
        original_filename="课程资料.pdf",
        file_format="pdf",
        status=DocumentStatus.READY,
    )
    session.add(document)
    session.flush()
    chunk = DocumentChunk(
        document_id=document.id,
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        chunk_index=0,
        content=content,
        chunk_metadata={},
    )
    session.add(chunk)
    session.commit()
    return chunk


def _persisted(session: Session, course: Course) -> list[Question]:
    """读取该课程下已落库的候选题。"""

    session.expire_all()
    return list(
        session.scalars(
            select(Question)
            .where(Question.course_id == course.id)
            .order_by(Question.created_at, Question.id)
        )
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


@pytest.fixture
def client_factory(session: Session) -> Generator[Any, None, None]:
    """装配可注入出题服务的 TestClient 工厂。"""

    with ExitStack() as stack:

        def factory(service: QuestionGenerationService | None = None) -> TestClient:
            with gr.Blocks() as ui:
                gr.Markdown("出题契约测试")
            app = create_app(settings=build_test_settings(), gradio_app=ui)

            def database() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = database
            if service is not None:
                app.dependency_overrides[get_question_generation_service] = lambda: service
            return stack.enter_context(TestClient(app))

        yield factory


# ---------------------------------------------------------------- 权限与路由


def test_generate_requires_teacher_permission(
    scenario: dict[str, Any], client_factory: Any
) -> None:
    """未认证 401；学生与管理员没有出题权限，返回 403。"""

    client = client_factory(_service_from_scenario(scenario))
    body = {"course_id": str(scenario["course"].id), "count": 1}

    assert client.post("/api/question-generation/candidates", json=body).status_code == 401
    for user, role in (
        (scenario["student"], UserRole.STUDENT),
        (scenario["admin"], UserRole.ADMIN),
    ):
        response = client.post(
            "/api/question-generation/candidates",
            headers=_headers(user, role),
            json=body,
        )
        assert response.status_code == 403, role


def test_routes_registered(client_factory: Any) -> None:
    """出题端点在应用装配后确实可用（避免模块写完但应用未加载）。"""

    client = client_factory()
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/question-generation/candidates" in paths
    assert "/api/question-generation/candidates/{candidate_id}" in paths
    assert "/api/question-generation/candidates/{candidate_id}/review" in paths
    assert set(paths["/api/question-generation/candidates"]) == {"get", "post"}


def _service_from_scenario(scenario: dict[str, Any]) -> QuestionGenerationService:
    """从场景取会话并构造替身服务（仅供权限用例使用）。"""

    session = scenario["session"]
    assert isinstance(session, Session)
    return _service(session)


# ---------------------------------------------------------------- 生成端


def test_generate_persists_batch_atomically_with_field_fidelity(scenario: Any) -> None:
    """整批落库：通过校验的存 Pending Review，未通过的存 Needs Revision，字段保真。"""

    session = scenario["session"]
    course = scenario["course"]
    teacher = scenario["teacher"]
    valid = make_candidate(
        content="变量的作用是什么？",
        difficulty="中等",
        knowledge_points=["变量", "作用域"],
        score=2.5,
    )
    broken = make_candidate(
        content="变量的作用域是什么？",
        scoring_rubric=UNUSABLE_RUBRIC,
        score=3,
    )

    response = _generate(
        session,
        course=course,
        teacher=teacher,
        candidates=[valid, broken],
    )

    assert response.generated_count == 2
    assert response.sources_persisted is False
    # 替身检索片段标识不是真实片段 UUID：明确计数为“无法解析”，不展示正文、不伪造来源。
    assert response.evidence == []
    assert response.unresolved_source_count == 1
    assert response.out_of_course_source_count == 0
    assert response.batch_validation.status is QuestionStatus.NEEDS_REVISION
    first, second = response.candidates
    assert first.status is QuestionStatus.PENDING_REVIEW
    assert first.validation.issues == []
    assert first.origin == "Candidate Generation"
    assert first.source_context_ids == ["chunk-1"]
    assert second.status is QuestionStatus.NEEDS_REVISION
    assert [issue.code for issue in second.validation.issues] == ["QUESTION_RUBRIC_UNUSABLE"]

    stored = {row.content: row for row in _persisted(session, course)}
    assert set(stored) == {valid.content, broken.content}
    row = stored[valid.content]
    assert row.status is QuestionStatus.PENDING_REVIEW
    assert row.course_id == course.id
    assert row.created_by == teacher.id
    assert row.type is QuestionType.SINGLE_CHOICE
    assert row.options == list(valid.options or [])
    assert row.reference_answer == valid.reference_answer
    assert row.scoring_rubric == valid.scoring_rubric
    assert row.difficulty == "中等"
    assert row.knowledge_points == ["变量", "作用域"]
    assert row.score == Decimal("2.50")
    assert stored[broken.content].status is QuestionStatus.NEEDS_REVISION
    assert all(
        item.status not in {QuestionStatus.APPROVED, QuestionStatus.PUBLISHED}
        for item in stored.values()
    )


def test_insufficient_context_returns_422_without_side_effects(scenario: Any) -> None:
    """检索上下文不足时返回 422，不创建任何候选题（不静默编造）。"""

    session = scenario["session"]
    course = scenario["course"]
    service = _service(session, candidates=[make_candidate()], chunks=[])

    with pytest.raises(qg.GenerationFailedError) as error:
        asyncio.run(
            service.generate_candidates(
                course_id=str(course.id),
                actor_id=str(scenario["teacher"].id),
                request_id="request-insufficient",
                count=1,
            )
        )
    assert error.value.error_code == QUESTION_INSUFFICIENT_CONTEXT
    assert _persisted(session, course) == []


def test_provider_not_ready_returns_503_without_side_effects(scenario: Any) -> None:
    """Provider 未就绪时报 503，且不产生任何候选题。"""

    pending = make_candidate()
    with pytest.raises(qg.GenerationFailedError) as error:
        _generate(
            scenario["session"],
            course=scenario["course"],
            teacher=scenario["teacher"],
            candidates=[pending],
            provider_error=ProviderNotReadyError("出题 Provider 未就绪。"),
        )

    assert error.value.error_code == QUESTION_PROVIDER_NOT_READY
    assert error.value.retryable is False
    assert _persisted(scenario["session"], scenario["course"]) == []


def test_batch_write_failure_rolls_back_whole_batch(
    scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写库中途失败时整批回滚，不留下半批候选题。"""

    calls = {"count": 0}
    real_plan = qg.plan_transition

    def _failing_plan(current: Any, target: Any, *, actor: Any = None) -> Any:
        calls["count"] += 1
        if calls["count"] == 2:
            raise CandidateStoreNotReadyError("模拟写入失败。")
        return real_plan(current, target, actor=actor)

    monkeypatch.setattr(qg, "plan_transition", _failing_plan)
    candidates = [make_candidate(content="第一题"), make_candidate(content="第二题")]

    with pytest.raises(CandidateStoreNotReadyError) as error:
        _generate(
            scenario["session"],
            course=scenario["course"],
            teacher=scenario["teacher"],
            candidates=candidates,
        )

    assert error.value.error_code == QUESTION_CANDIDATE_STORE_NOT_READY
    assert _persisted(scenario["session"], scenario["course"]) == []


def test_generation_evidence_is_course_scoped(scenario: Any) -> None:
    """检索依据按课程二次校验：只返回本课程片段，其他课程片段只计数。"""

    session = scenario["session"]
    course = scenario["course"]
    teacher = scenario["teacher"]
    own_chunk = _seed_chunk(session, course=course, teacher=teacher, content="变量用于保存数据。")
    other_chunk = _seed_chunk(
        session,
        course=scenario["other_course"],
        teacher=scenario["other_teacher"],
        content="其他课程的片段。",
    )
    candidate = make_candidate(source_context_ids=[str(own_chunk.id), str(other_chunk.id)])

    response = _generate(
        session,
        course=course,
        teacher=teacher,
        candidates=[candidate],
        chunks=[
            make_chunk(str(own_chunk.id)),
            make_chunk(str(other_chunk.id), document_id=str(other_chunk.document_id)),
        ],
    )

    assert response.out_of_course_source_count == 1
    assert response.unresolved_source_count == 0
    assert [item.chunk_id for item in response.evidence] == [str(own_chunk.id)]
    assert response.evidence[0].content == "变量用于保存数据。"
    assert response.evidence[0].source_file == "课程资料.pdf"
    assert response.evidence[0].chunk_index == 0
    assert response.candidates[0].source_context_ids == [
        str(own_chunk.id),
        str(other_chunk.id),
    ]


def test_generation_rejects_cross_course_teacher(scenario: Any, client_factory: Any) -> None:
    """教师只能对自己拥有的课程出题，跨课程返回 403。"""

    client = client_factory(_service_from_scenario(scenario))
    response = client.post(
        "/api/question-generation/candidates",
        headers=_headers(scenario["other_teacher"], UserRole.TEACHER),
        json={"course_id": str(scenario["course"].id), "count": 1},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "QUESTION_CANDIDATE_PERMISSION_DENIED"


def test_generate_endpoint_returns_persisted_candidates(
    scenario: Any, client_factory: Any
) -> None:
    """端点返回 201 与真实候选题载荷，请求追踪标识可用请求头传递。"""

    service = _service(scenario["session"], candidates=[make_candidate()])
    client = client_factory(service)

    response = client.post(
        "/api/question-generation/candidates",
        headers={
            **_headers(scenario["teacher"], UserRole.TEACHER),
            "X-Request-ID": "request-from-header",
        },
        json={
            "course_id": str(scenario["course"].id),
            "knowledge_points": ["变量"],
            "difficulty": "中等",
            "question_type": QuestionType.SINGLE_CHOICE.value,
            "count": 1,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["request_id"] == "request-from-header"
    assert body["generated_count"] == 1
    assert body["candidates"][0]["status"] == QuestionStatus.PENDING_REVIEW.value
    assert body["candidates"][0]["origin"] == "Candidate Generation"
    assert body["sources_persisted"] is False
    assert len(_persisted(scenario["session"], scenario["course"])) == 1


def test_generate_validation_error_returns_422(scenario: Any, client_factory: Any) -> None:
    """请求体非法（数量越界）由请求校验拒绝，返回 422 且不写库。"""

    client = client_factory(_service_from_scenario(scenario))
    response = client.post(
        "/api/question-generation/candidates",
        headers=_headers(scenario["teacher"], UserRole.TEACHER),
        json={"course_id": str(scenario["course"].id), "count": 0},
    )

    assert response.status_code == 422
    assert _persisted(scenario["session"], scenario["course"]) == []


# ---------------------------------------------------------------- 查询端


def test_candidate_list_scopes_to_teacher_courses(scenario: Any, client_factory: Any) -> None:
    """候选题列表按教师课程隔离，支持状态过滤与分页回显。"""

    session = scenario["session"]
    _generate(
        session,
        course=scenario["course"],
        teacher=scenario["teacher"],
        candidates=[make_candidate(content="自己的题")],
    )
    _generate(
        session,
        course=scenario["other_course"],
        teacher=scenario["other_teacher"],
        candidates=[make_candidate(content="别人的题", scoring_rubric=UNUSABLE_RUBRIC)],
    )
    client = client_factory(_service_from_scenario(scenario))
    headers = _headers(scenario["teacher"], UserRole.TEACHER)

    page = client.get("/api/question-generation/candidates", headers=headers)
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 1
    assert body["limit"] == qg.DEFAULT_PAGE_SIZE
    assert body["offset"] == 0
    assert [item["content"] for item in body["items"]] == ["自己的题"]

    filtered = client.get(
        "/api/question-generation/candidates",
        headers=headers,
        params={"candidate_status": QuestionStatus.NEEDS_REVISION.value},
    )
    assert filtered.json()["total"] == 0

    invalid = client.get(
        "/api/question-generation/candidates", headers=headers, params={"limit": 0}
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["error_code"] == "QUESTION_GENERATION_INVALID_PAGE"


def test_candidate_detail_isolated_between_courses(scenario: Any, client_factory: Any) -> None:
    """跨课程读取单个候选题返回 404，不泄露其他教师的题目。"""

    session = scenario["session"]
    response = _generate(
        session,
        course=scenario["course"],
        teacher=scenario["teacher"],
        candidates=[make_candidate()],
    )
    candidate_id = response.candidates[0].candidate_id
    client = client_factory(_service_from_scenario(scenario))

    own = client.get(
        f"/api/question-generation/candidates/{candidate_id}",
        headers=_headers(scenario["teacher"], UserRole.TEACHER),
    )
    other = client.get(
        f"/api/question-generation/candidates/{candidate_id}",
        headers=_headers(scenario["other_teacher"], UserRole.TEACHER),
    )

    assert own.status_code == 200
    assert own.json()["candidate_id"] == candidate_id
    assert other.status_code == 404
    assert other.json()["detail"]["error_code"] == "QUESTION_CANDIDATE_NOT_FOUND"


# ---------------------------------------------------------------- 教师审核


def _pending_candidate_id(session: Session, course: Course, teacher: User) -> str:
    """生成一条待审核候选题并返回其标识。"""

    response = _generate(
        session,
        course=course,
        teacher=teacher,
        candidates=[make_candidate()],
    )
    return str(response.candidates[0].candidate_id)


def test_review_approve_then_conflict(scenario: Any, client_factory: Any) -> None:
    """审核通过进入 Approved；再次审核返回 409（只有待审核可审核）。"""

    session = scenario["session"]
    candidate_id = _pending_candidate_id(session, scenario["course"], scenario["teacher"])
    client = client_factory(_service_from_scenario(scenario))
    headers = _headers(scenario["teacher"], UserRole.TEACHER)

    approved = client.post(
        f"/api/question-generation/candidates/{candidate_id}/review",
        headers=headers,
        json={"action": "approve"},
    )

    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == QuestionStatus.APPROVED.value
    assert body["previous_status"] == QuestionStatus.PENDING_REVIEW.value
    assert body["decision"] == "approve"
    assert body["comment_persisted"] is False
    assert body["request_id"]
    stored = session.get(Question, UUID(candidate_id))
    assert stored is not None and stored.status is QuestionStatus.APPROVED

    again = client.post(
        f"/api/question-generation/candidates/{candidate_id}/review",
        headers=headers,
        json={"action": "approve"},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["error_code"] == QUESTION_CANDIDATE_NOT_PENDING_REVIEW

    published = client.post(
        f"/api/question-generation/candidates/{candidate_id}/review",
        headers=headers,
        json={"action": "publish"},
    )
    assert published.status_code == 422


def test_review_revision_requires_comment_and_transitions(scenario: Any, client_factory: Any) -> None:
    """退回修订必须给出意见；给出后状态变为 Needs Revision。"""

    session = scenario["session"]
    candidate_id = _pending_candidate_id(session, scenario["course"], scenario["teacher"])
    client = client_factory(_service_from_scenario(scenario))
    headers = _headers(scenario["teacher"], UserRole.TEACHER)

    missing = client.post(
        f"/api/question-generation/candidates/{candidate_id}/review",
        headers=headers,
        json={"action": "request_revision", "comment": "   "},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["error_code"] == QUESTION_REVISION_COMMENT_REQUIRED

    revised = client.post(
        f"/api/question-generation/candidates/{candidate_id}/review",
        headers=headers,
        json={"action": "request_revision", "comment": "评分标准需要拆出步骤分。"},
    )
    assert revised.status_code == 200
    assert revised.json()["status"] == QuestionStatus.NEEDS_REVISION.value
    assert revised.json()["comment"] == "评分标准需要拆出步骤分。"
    assert revised.json()["comment_persisted"] is False


def test_review_rejects_stale_expected_status(scenario: Any, client_factory: Any) -> None:
    """预检基准与实际状态不一致时返回 409，不做陈旧覆盖。"""

    session = scenario["session"]
    candidate_id = _pending_candidate_id(session, scenario["course"], scenario["teacher"])
    client = client_factory(_service_from_scenario(scenario))

    stale = client.post(
        f"/api/question-generation/candidates/{candidate_id}/review",
        headers=_headers(scenario["teacher"], UserRole.TEACHER),
        json={
            "action": "approve",
            "expected_status": QuestionStatus.APPROVED.value,
        },
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["error_code"] == QUESTION_CANDIDATE_STALE
    stored = session.get(Question, UUID(candidate_id))
    assert stored is not None and stored.status is QuestionStatus.PENDING_REVIEW


def test_review_requires_teacher_and_course_ownership(
    scenario: Any, client_factory: Any
) -> None:
    """学生/管理员无审核权限（403）；跨课程审核返回 404。"""

    session = scenario["session"]
    candidate_id = _pending_candidate_id(session, scenario["course"], scenario["teacher"])
    client = client_factory(_service_from_scenario(scenario))
    body = {"action": "approve"}

    for user, role in (
        (scenario["student"], UserRole.STUDENT),
        (scenario["admin"], UserRole.ADMIN),
    ):
        response = client.post(
            f"/api/question-generation/candidates/{candidate_id}/review",
            headers=_headers(user, role),
            json=body,
        )
        assert response.status_code == 403, role

    other = client.post(
        f"/api/question-generation/candidates/{candidate_id}/review",
        headers=_headers(scenario["other_teacher"], UserRole.TEACHER),
        json=body,
    )
    assert other.status_code == 404
