"""T075 AI 出题视图测试：候选列表展示、审核回调与失败提示。

TCR（2026-09-18，T075 / 评审 S01、S05、B11）：

- **既有覆盖缺口**：T098 只交付了“未接线即不可用”的占位视图，没有任何用例证明候选题能按
  权威状态展示、审核回调会真的提交教师结论、失败时不会伪造候选题。
- **新增用例**：候选列表行与权威状态标签、空课程/空列表空态、生成成功后的列表与按钮状态、
  检索不足横幅与按钮禁用、链路未就绪提示、选中候选题预览、审核通过与退回修订回调、
  退回缺意见时不调用服务、冲突时列表事实不变。
- **对外契约**：FR-024～FR-028、T075 API 响应 DTO、T068 ``Pending Review → Approved``/
  ``Needs Revision``。

视图只依赖 ``question_generation_loaders`` 协议；本文件注入替身加载器，不访问数据库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from backend.app.api.question_generation import (
    GENERATION_ORIGIN,
    CandidateDTO,
    CandidateGenerationResponse,
    CandidateNotFoundError,
    CandidatePageDTO,
    CandidateReviewOutcomeDTO,
    CandidateValidationDTO,
    GeneratedCandidateDTO,
    GenerationEvidenceDTO,
    QuestionGenerationError,
)
from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.ui import question_generation_view as view

#: 学生会话状态：用于验证视图守卫。
_STUDENT_STATE: dict[str, Any] = {
    "access_token": "token",
    "user_id": "student-1",
    "roles": ["Student"],
}
#: 教师会话状态。
_TEACHER_STATE: dict[str, Any] = {
    "access_token": "token",
    "user_id": "teacher-1",
    "roles": ["Teacher"],
}


def _candidate(
    candidate_id: str = "candidate-1",
    *,
    status: QuestionStatus = QuestionStatus.PENDING_REVIEW,
    content: str = "变量的作用是什么？",
) -> CandidateDTO:
    """构造一条候选题展示 DTO。"""

    return CandidateDTO(
        candidate_id=candidate_id,
        course_id="course-1",
        question_type=QuestionType.SINGLE_CHOICE,
        content=content,
        options=["保存数据", "删除数据"],
        reference_answer="保存数据",
        scoring_rubric="选择正确选项得 2 分。",
        difficulty="中等",
        knowledge_points=["变量"],
        score=Decimal("2.00"),
        status=status,
        created_at=datetime(2026, 9, 18, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 18, 9, 0, tzinfo=UTC),
    )


def _page(*items: CandidateDTO) -> CandidatePageDTO:
    """构造候选列表分页结果。"""

    return CandidatePageDTO(total=len(items), limit=50, offset=0, items=list(items))


class _StubLoaders:
    """出题加载器替身：只记录调用并返回注入结果。"""

    def __init__(
        self,
        *,
        courses: Sequence[tuple[str, str]] = (("Python 基础", "course-1"),),
        page: CandidatePageDTO | None = None,
        generation: CandidateGenerationResponse | None = None,
        outcome: CandidateReviewOutcomeDTO | None = None,
        generation_error: Exception | None = None,
        page_error: Exception | None = None,
        review_error: Exception | None = None,
    ) -> None:
        self.courses = list(courses)
        self.page = page if page is not None else _page(_candidate())
        self.generation = generation
        self.outcome = outcome
        self.generation_error = generation_error
        self.page_error = page_error
        self.review_error = review_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def load_courses(self, state: Mapping[str, Any] | None) -> list[tuple[str, str]]:
        self.calls.append(("courses", {}))
        return list(self.courses)

    async def generate(self, state: Mapping[str, Any] | None, **kwargs: Any) -> Any:
        self.calls.append(("generate", dict(kwargs)))
        if self.generation_error is not None:
            raise self.generation_error
        return self.generation

    def list_candidates(self, state: Mapping[str, Any] | None, **kwargs: Any) -> Any:
        self.calls.append(("list", dict(kwargs)))
        if self.page_error is not None:
            raise self.page_error
        return self.page

    def candidate_detail(self, candidate_id: str, state: Any) -> Any:
        self.calls.append(("detail", {"candidate_id": candidate_id}))
        return _candidate(candidate_id)

    def review(self, state: Mapping[str, Any] | None, **kwargs: Any) -> Any:
        self.calls.append(("review", dict(kwargs)))
        if self.review_error is not None:
            raise self.review_error
        return self.outcome


def _install(loaders: _StubLoaders) -> None:
    """把替身加载器注入视图。"""

    view.configure_question_generation_loaders(
        courses=loaders.load_courses,
        generate=loaders.generate,
        list_candidates=loaders.list_candidates,
        candidate_detail=loaders.candidate_detail,
        review=loaders.review,
    )


@pytest.fixture(autouse=True)
def reset_loaders() -> Any:
    """每个用例前后恢复默认接线。"""

    view.configure_question_generation_loaders()
    yield
    view.configure_question_generation_loaders()


def _update_value(update: Any) -> Any:
    """读取 Gradio 组件更新的字段值。"""

    if isinstance(update, Mapping):
        return update.get("interactive")
    return getattr(update, "interactive", None)


# ---------------------------------------------------------------- 列表与守卫


def test_candidate_rows_use_authoritative_status() -> None:
    """列表行展示题干、题型、权威审核状态、分值与知识点。"""

    rows = view.candidate_rows(_page(_candidate(), _candidate("candidate-2")))

    assert rows[0][0] == "变量的作用是什么？"
    assert rows[0][1] == QuestionType.SINGLE_CHOICE.value
    assert "待教师审核" in rows[0][2]
    assert rows[0][3] == "2.00"
    assert rows[0][4] == "变量"
    assert len(rows) == 2


def test_refresh_candidate_list_without_course_is_warning() -> None:
    """未选择课程时给出提示，不调用服务。"""

    loaders = _StubLoaders()
    _install(loaders)

    rows, items, message = view.refresh_candidate_list("", "", _TEACHER_STATE)

    assert rows == [] and items == []
    assert "请先选择课程" in message
    assert loaders.calls == []


def test_refresh_candidate_list_empty_page_is_explicit() -> None:
    """没有候选题时给出明确空态，不伪造数据。"""

    loaders = _StubLoaders(page=_page())
    _install(loaders)

    rows, items, message = view.refresh_candidate_list("course-1", "", _TEACHER_STATE)

    assert rows == [] and items == []
    assert view.NO_CANDIDATE_MESSAGE in message


def test_refresh_candidate_list_passes_filters_and_returns_rows() -> None:
    """状态筛选与课程标识透传给加载器，返回真实行。"""

    loaders = _StubLoaders(page=_page(_candidate()))
    _install(loaders)

    rows, items, message = view.refresh_candidate_list(
        "course-1", QuestionStatus.PENDING_REVIEW.value, _TEACHER_STATE
    )

    assert rows[0][0] == "变量的作用是什么？"
    assert items[0]["candidate_id"] == "candidate-1"
    assert "共 1 条候选题" in message
    assert loaders.calls[0][1] == {
        "course_id": "course-1",
        "candidate_status": QuestionStatus.PENDING_REVIEW,
    }


def test_view_guard_rejects_student_session() -> None:
    """学生会话被界面守卫拒绝，且不调用加载器。"""

    loaders = _StubLoaders()
    _install(loaders)

    rows, items, message = view.refresh_candidate_list("course-1", "", _STUDENT_STATE)

    assert rows == [] and items == []
    assert "无权访问" in message
    assert loaders.calls == []


# ---------------------------------------------------------------- 生成


def _generation_response(*candidates: GeneratedCandidateDTO) -> CandidateGenerationResponse:
    """构造生成响应。"""

    return CandidateGenerationResponse(
        request_id="request-1",
        course_id="course-1",
        generated_count=len(candidates),
        candidates=list(candidates),
        batch_validation=CandidateValidationDTO(status=QuestionStatus.PENDING_REVIEW),
        evidence=[
            GenerationEvidenceDTO(
                chunk_id="chunk-1",
                course_id="course-1",
                document_id="doc-1",
                source_file="课程资料.pdf",
                chunk_index=0,
                content="变量用于保存数据。",
            )
        ],
        sources_persisted=False,
        model="stub-model",
        prompt_version="question-generation-v1",
    )


def _generated(status: QuestionStatus, *, issues: Sequence[Any] = ()) -> GeneratedCandidateDTO:
    """构造生成响应中的候选题。"""

    base = _candidate(status=status)
    return GeneratedCandidateDTO(
        **base.model_dump(),
        origin=GENERATION_ORIGIN,
        validation=CandidateValidationDTO(status=status, issues=list(issues)),
        source_context_ids=["chunk-1"],
        sources_persisted=False,
    )


def test_generate_candidates_returns_rows_preview_and_buttons() -> None:
    """生成成功后刷新列表、预览、审核按钮与检索依据。"""

    loaders = _StubLoaders(generation=_generation_response(_generated(QuestionStatus.PENDING_REVIEW)))
    _install(loaders)

    rows, items, status, preview, approve, revision, evidence, message = asyncio.run(
        view.generate_candidates(
            "course-1", "变量", "中等", QuestionType.SINGLE_CHOICE.value, 1, _TEACHER_STATE
        )
    )

    assert rows[0][0] == "变量的作用是什么？"
    assert items[0]["origin"] == GENERATION_ORIGIN
    assert "待审核 1 道" in status
    assert "参考答案" in preview
    assert _update_value(approve) is True
    assert _update_value(revision) is True
    assert "变量用于保存数据。" in evidence
    assert "课程资料.pdf" in evidence
    assert "未落库" in evidence
    assert status == message
    assert loaders.calls[0][1]["knowledge_points"] == ["变量"]


def test_generate_candidates_without_course_is_warning() -> None:
    """未选择课程时拒绝生成，不调用服务。"""

    loaders = _StubLoaders(generation=_generation_response(_generated(QuestionStatus.PENDING_REVIEW)))
    _install(loaders)

    rows, items, status, _preview, approve, revision, evidence, message = asyncio.run(
        view.generate_candidates("", "", None, "", 1, _TEACHER_STATE)
    )

    assert rows == [] and items == []
    assert "请先选择课程" in status
    assert _update_value(approve) is False
    assert _update_value(revision) is False
    assert evidence == ""
    assert message == status
    assert loaders.calls == []


def test_insufficient_context_shows_banner_and_disables_actions() -> None:
    """检索上下文不足时显示明确横幅，并禁用审核操作。"""

    loaders = _StubLoaders(
        generation_error=QuestionGenerationError(
            "检索上下文不足。", error_code="QUESTION_INSUFFICIENT_CONTEXT"
        )
    )
    _install(loaders)

    rows, items, status, _preview, approve, revision, _evidence, message = asyncio.run(
        view.generate_candidates(
            "course-1", "变量", None, "", 1, _TEACHER_STATE
        )
    )

    assert rows == [] and items == []
    assert view.INSUFFICIENT_CONTEXT_MESSAGE in status
    assert _update_value(approve) is False
    assert _update_value(revision) is False
    assert message == status


def test_not_ready_dependency_shows_unavailable_message() -> None:
    """Provider/Embedding 未就绪时显示链路未就绪提示与错误码。"""

    loaders = _StubLoaders(
        generation_error=QuestionGenerationError(
            "出题 Provider 未就绪。", error_code="QUESTION_PROVIDER_NOT_READY"
        )
    )
    _install(loaders)

    _rows, _items, status, _preview, _approve, _revision, _evidence, message = asyncio.run(
        view.generate_candidates("course-1", "", None, "", 1, _TEACHER_STATE)
    )

    assert view.GENERATION_UNAVAILABLE_MESSAGE in status
    assert "QUESTION_PROVIDER_NOT_READY" in status
    assert message == status


# ---------------------------------------------------------------- 选中与审核


class _SelectEvent:
    """模拟 Gradio 选择事件。"""

    def __init__(self, index: int) -> None:
        self.index = index


def test_select_candidate_preview_and_action_state() -> None:
    """选中待审核候选题时可审核；其他状态给出门禁提示。"""

    pending = _candidate()
    approved = _candidate("candidate-2", status=QuestionStatus.APPROVED)
    loaders = _StubLoaders(page=_page(pending, approved))
    _install(loaders)
    _rows, items, _message = view.refresh_candidate_list("course-1", "", _TEACHER_STATE)

    preview, selected, approve, revision, message = view.select_candidate(
        _SelectEvent(0), None, items, _TEACHER_STATE
    )
    assert "参考答案" in preview
    assert selected is not None and selected["candidate_id"] == "candidate-1"
    assert _update_value(approve) is True
    assert _update_value(revision) is True
    assert "待教师审核" in message

    _preview, _selected, approve2, revision2, message2 = view.select_candidate(
        _SelectEvent(1), None, items, _TEACHER_STATE
    )
    assert _update_value(approve2) is False
    assert _update_value(revision2) is False
    assert message2 == view.NOT_PENDING_REVIEW_MESSAGE or view.NOT_PENDING_REVIEW_MESSAGE in message2


def test_select_candidate_without_items_is_empty_state() -> None:
    """没有候选题时不返回选中项，操作保持禁用。"""

    _install(_StubLoaders())

    preview, selected, approve, revision, message = view.select_candidate(
        _SelectEvent(0), None, [], _TEACHER_STATE
    )

    assert "尚未选择" in preview
    assert selected is None
    assert _update_value(approve) is False
    assert _update_value(revision) is False
    assert "未选中" in message


def test_approve_submits_decision_and_refreshes_list() -> None:
    """审核通过回调提交真实结论并按最新列表刷新。"""

    loaders = _StubLoaders(
        page=_page(_candidate(status=QuestionStatus.APPROVED)),
        outcome=CandidateReviewOutcomeDTO(
            candidate_id="candidate-1",
            decision="approve",
            previous_status=QuestionStatus.PENDING_REVIEW,
            status=QuestionStatus.APPROVED,
            comment_persisted=False,
            request_id="request-1",
        ),
    )
    _install(loaders)

    message, rows, items, preview, approve, revision = view.submit_candidate_review_action(
        "approve",
        {"candidate_id": "candidate-1", "status": QuestionStatus.PENDING_REVIEW.value},
        "",
        "course-1",
        "",
        _TEACHER_STATE,
    )

    assert "审核通过" in message
    assert rows[0][0] == "变量的作用是什么？"
    assert items[0]["status"] == QuestionStatus.APPROVED.value
    assert "尚未选择" in preview
    assert _update_value(approve) is False
    assert _update_value(revision) is False
    assert loaders.calls[0][1]["action"] == "approve"
    assert loaders.calls[0][1]["candidate_id"] == "candidate-1"
    assert loaders.calls[1][0] == "list"


def test_revision_requires_comment_before_calling_service() -> None:
    """退回修订缺少意见时直接提示，不调用服务。"""

    loaders = _StubLoaders()
    _install(loaders)

    message, _rows, _items, _preview, _approve, _revision = (
        view.submit_candidate_review_action(
            "request_revision",
            {"candidate_id": "candidate-1", "status": QuestionStatus.PENDING_REVIEW.value},
            "   ",
            "course-1",
            "",
            _TEACHER_STATE,
        )
    )

    assert "必须填写修订意见" in message
    assert all(call[0] != "review" for call in loaders.calls)


def test_revision_with_comment_reports_not_persisted() -> None:
    """退回修订成功后明确说明意见未落库。"""

    loaders = _StubLoaders(
        page=_page(_candidate(status=QuestionStatus.NEEDS_REVISION)),
        outcome=CandidateReviewOutcomeDTO(
            candidate_id="candidate-1",
            decision="request_revision",
            previous_status=QuestionStatus.PENDING_REVIEW,
            status=QuestionStatus.NEEDS_REVISION,
            comment="请拆分评分标准。",
            comment_persisted=False,
            request_id="request-1",
        ),
    )
    _install(loaders)

    message, _rows, items, _preview, _approve, _revision = (
        view.submit_candidate_review_action(
            "request_revision",
            {"candidate_id": "candidate-1", "status": QuestionStatus.PENDING_REVIEW.value},
            "请拆分评分标准。",
            "course-1",
            "",
            _TEACHER_STATE,
        )
    )

    assert "退回修订" in message
    assert view.COMMENT_NOT_PERSISTED_NOTE in message
    assert items[0]["status"] == QuestionStatus.NEEDS_REVISION.value
    assert loaders.calls[0][1]["comment"] == "请拆分评分标准。"


def test_conflict_keeps_list_facts_unchanged() -> None:
    """状态冲突时保留列表事实并给出错误提示，不假装成功。"""

    loaders = _StubLoaders(
        page=_page(_candidate(status=QuestionStatus.APPROVED)),
        review_error=CandidateNotFoundError("候选题不存在。"),
    )
    _install(loaders)

    message, rows, items, _preview, approve, revision = view.submit_candidate_review_action(
        "approve",
        {"candidate_id": "candidate-1", "status": QuestionStatus.PENDING_REVIEW.value},
        "",
        "course-1",
        "",
        _TEACHER_STATE,
    )

    assert "QUESTION_CANDIDATE_NOT_FOUND" in message
    assert rows[0][0] == "变量的作用是什么？"
    assert items[0]["status"] == QuestionStatus.APPROVED.value
    assert _update_value(approve) is False
    assert _update_value(revision) is False


def test_refresh_generation_context_lists_real_courses() -> None:
    """课程下拉使用真实课程；无课程时给出明确提示。"""

    loaders = _StubLoaders(courses=(("Python 基础", "course-1"),))
    _install(loaders)

    update, message = view.refresh_generation_context(_TEACHER_STATE)

    assert isinstance(update, Mapping) and update.get("choices") == [("Python 基础", "course-1")]
    assert "请选择课程" in message

    empty = _StubLoaders(courses=())
    _install(empty)
    update, message = view.refresh_generation_context(_TEACHER_STATE)
    assert update.get("choices") == []
    assert "暂无课程" in message
