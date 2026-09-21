"""T077 复核视图测试：队列展示、双栏详情、检索依据与决策回调。

TCR（2026-09-18，T077 / 评审 S01、S05、B08、B10）：

- **既有覆盖缺口**：T096 只交付了“未接线即不可用”的占位复核工作台，没有任何用例证明
  队列与详情来自持久化事实、检索依据按课程展示、教师确认/修改会真的提交结论，
  也没有覆盖“决定已保存但恢复未完成”的部分成功提示。
- **新增用例**：队列行映射与权威状态标签、空队列空态、学生会话守卫、详情字段与置信度/
  待复核横幅、检索依据与引用计数说明、非待复核状态禁用操作、下一条切换、
  确认回调提交并刷新、修改缺理由时不调用服务、修改提交字段、部分成功与冲突提示。
- **对外契约**：FR-036/FR-037、T077 API 响应 DTO、T074 ``ReviewOutcome`` 部分成功语义。

视图只依赖 ``review_loaders`` 协议；本文件注入替身加载器，不访问数据库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import gradio as gr
import pytest
from gradio.state_holder import SessionState

import backend.app.ui.gradio_app as gradio_app_module
from backend.app.api.reviews import (
    ReviewDecisionOutcomeDTO,
    ReviewDetailDTO,
    ReviewEvidenceDTO,
    ReviewQueryError,
    ReviewQueueItemDTO,
    ReviewQueuePageDTO,
)
from backend.app.domain.enums import QuestionType, ReviewStatus, WorkflowStatus
from backend.app.ui import review_view as view
from backend.app.ui.gradio_app import create_gradio_app

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


def _queue_item(
    answer_id: str = "answer-1",
    *,
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW,
    question_number: int = 2,
) -> ReviewQueueItemDTO:
    """构造一条队列记录。"""

    return ReviewQueueItemDTO(
        submission_id="submission-1",
        answer_id=answer_id,
        course_id="course-1",
        exam_id="exam-1",
        exam_title="Python 阶段测验",
        student_id="student-1",
        student_name="学生甲",
        question_id="question-1",
        question_number=question_number,
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal("10.00"),
        score=Decimal("6.00"),
        confidence=0.3,
        review_status=review_status,
        requires_review=review_status is ReviewStatus.PENDING_REVIEW,
        updated_at=datetime(2026, 9, 18, 9, 30, tzinfo=UTC),
    )


def _detail(
    *,
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW,
    answer_id: str = "answer-1",
) -> ReviewDetailDTO:
    """构造一条复核详情。"""

    return ReviewDetailDTO(
        submission_id="submission-1",
        answer_id=answer_id,
        course_id="course-1",
        exam_id="exam-1",
        exam_title="Python 阶段测验",
        student_id="student-1",
        student_name="学生甲",
        question_id="question-1",
        question_number=2,
        question_type=QuestionType.SHORT_ANSWER,
        question_content="解释变量的作用。",
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明保存和引用数据即可。",
        max_score=Decimal("10.00"),
        student_answer="变量用于保存数据。",
        score=Decimal("6.00"),
        reason="说明了变量的作用。",
        knowledge_points=["变量"],
        missing_knowledge_points=["引用数据"],
        correct_points=["保存数据"],
        suggestions=["补充变量引用。"],
        confidence=0.3,
        validation_status="Validated",
        review_status=review_status,
        requires_review=review_status is ReviewStatus.PENDING_REVIEW,
        retrieved_context_ids=["chunk-1"],
        evidence=[
            ReviewEvidenceDTO(
                chunk_id="chunk-1",
                course_id="course-1",
                document_id="doc-1",
                source_file="课程资料.pdf",
                chunk_index=0,
                content="变量用于保存数据，并可在后续语句中引用。",
            )
        ],
        unresolved_evidence_count=1,
        out_of_course_evidence_count=1,
        workflow_id="grading-1",
        thread_id="thread-1",
        workflow_status=WorkflowStatus.PAUSED,
        resumable=True,
        review_records=[],
        updated_at=datetime(2026, 9, 18, 9, 30, tzinfo=UTC),
    )


def _outcome(
    *,
    resume_status: str = "succeeded",
    resume_error_code: str | None = None,
) -> ReviewDecisionOutcomeDTO:
    """构造决策回执。"""

    return ReviewDecisionOutcomeDTO(
        workflow_id="grading-1",
        submission_id="submission-1",
        answer_id="answer-1",
        decision=ReviewStatus.CONFIRMED,
        decision_saved=True,
        review_record_id="record-1",
        resume_status=resume_status,  # type: ignore[arg-type]
        resume_error_code=resume_error_code,
        workflow_status=WorkflowStatus.COMPLETED,
        pending_review_count=0,
        resumable=False,
        exam_result_persisted=True,
        message="教师结论已生效。",
    )


class _StubLoaders:
    """复核加载器替身：记录调用并返回注入结果。"""

    def __init__(
        self,
        *,
        page: ReviewQueuePageDTO | None = None,
        detail_factory: Any | None = None,
        outcome: ReviewDecisionOutcomeDTO | None = None,
        queue_error: Exception | None = None,
        decision_error: Exception | None = None,
    ) -> None:
        self.page = (
            page
            if page is not None
            else ReviewQueuePageDTO(total=1, limit=50, offset=0, items=[_queue_item()])
        )
        self.detail_factory = (
            detail_factory
            if detail_factory is not None
            else (lambda answer_id: _detail(answer_id=answer_id or "answer-1"))
        )
        self.outcome = outcome if outcome is not None else _outcome()
        self.queue_error = queue_error
        self.decision_error = decision_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def queue(self, state: Mapping[str, Any] | None, **kwargs: Any) -> Any:
        self.calls.append(("queue", dict(kwargs)))
        if self.queue_error is not None:
            raise self.queue_error
        return self.page

    def detail_loader(self, state: Mapping[str, Any] | None, **kwargs: Any) -> Any:
        self.calls.append(("detail", dict(kwargs)))
        return self.detail_factory(kwargs.get("answer_id"))

    async def decision(self, state: Mapping[str, Any] | None, **kwargs: Any) -> Any:
        self.calls.append(("decision", dict(kwargs)))
        if self.decision_error is not None:
            raise self.decision_error
        return self.outcome


def _install(loaders: _StubLoaders) -> None:
    """把替身加载器注入视图。"""

    view.configure_review_loaders(
        queue=loaders.queue,
        detail=loaders.detail_loader,
        decision=loaders.decision,
    )


@pytest.fixture(autouse=True)
def reset_loaders() -> Any:
    """每个用例前后恢复默认接线。"""

    view.configure_review_loaders()
    yield
    view.configure_review_loaders()


class _SelectEvent:
    """模拟 Gradio 选择事件。"""

    def __init__(self, index: int | tuple[int, int]) -> None:
        self.index = index


def _interactive(update: Any) -> Any:
    """读取组件更新的可交互状态。"""

    if isinstance(update, Mapping):
        return update.get("interactive")
    return getattr(update, "interactive", None)


# ---------------------------------------------------------------- 队列


def test_review_queue_rows_use_authoritative_status() -> None:
    """队列行展示学生、考试、题号与权威复核状态。"""

    rows = view.review_queue_rows(
        ReviewQueuePageDTO(total=2, limit=50, offset=0, items=[_queue_item(), _queue_item("a-2")])
    )

    assert rows[0][0] == "学生甲"
    assert rows[0][1] == "Python 阶段测验"
    assert rows[0][2] == "第 2 题"
    assert "待人工复核" in rows[0][3]
    assert len(rows) == 2


def test_refresh_queue_returns_rows_and_items() -> None:
    """刷新队列返回真实行与可选中条目。"""

    loaders = _StubLoaders()
    _install(loaders)

    rows, items, message = view.refresh_review_queue("", "", "", _TEACHER_STATE)

    assert rows[0][0] == "学生甲"
    assert items[0]["answer_id"] == "answer-1"
    assert "共 1 条复核记录" in message
    assert loaders.calls[0][1] == {"exam_id": None, "student_id": None, "review_status": None}


def test_refresh_queue_empty_is_explicit() -> None:
    """没有记录时给出明确空态。"""

    loaders = _StubLoaders(
        page=ReviewQueuePageDTO(total=0, limit=50, offset=0, items=[])
    )
    _install(loaders)

    rows, items, message = view.refresh_review_queue("", "", "Pending Review", _TEACHER_STATE)

    assert rows == [] and items == []
    assert view.NO_QUEUE_ITEM_MESSAGE in message


def test_refresh_queue_rejects_student_session() -> None:
    """学生会话被界面守卫拒绝，且不调用加载器。"""

    loaders = _StubLoaders()
    _install(loaders)

    rows, items, message = view.refresh_review_queue("", "", "", _STUDENT_STATE)

    assert rows == [] and items == []
    assert "无权访问" in message
    assert loaders.calls == []


# ---------------------------------------------------------------- 详情


def _items() -> list[dict[str, Any]]:
    """构造队列条目状态。"""

    return [_queue_item().model_dump(mode="json"), _queue_item("a-2").model_dump(mode="json")]


@pytest.fixture(scope="module")
def registered_review_callback() -> tuple[gr.Blocks, Any]:
    """从完整应用取得经过认证守卫包装的复核选择回调。"""

    app = create_gradio_app()
    callbacks = [
        block_fn
        for block_fn in app.fns.values()
        if getattr(block_fn.fn, "__name__", None) == "select_review_item"
    ]
    assert len(callbacks) == 1
    return app, callbacks[0]


def _run_registered_select(
    app: gr.Blocks,
    callback: Any,
    *,
    items: list[dict[str, Any]],
    index: Any,
    selected: bool = True,
) -> tuple[dict[str, Any], SessionState]:
    """经 Gradio 预处理和事件注入执行已注册回调。"""

    state = SessionState(app)
    state[callback.inputs[0]._id] = items
    state[callback.inputs[1]._id] = _TEACHER_STATE
    event = gr.EventData(
        None,
        {
            "index": index,
            "value": None,
            "row_value": None,
            "col_value": None,
            "selected": selected,
        },
    )
    response = asyncio.run(
        app.process_api(
            callback,
            [None, None],
            state=state,
            event_data=event,
        )
    )
    return response, state


def test_registered_select_uses_row_from_two_dimensional_index(
    registered_review_callback: tuple[gr.Blocks, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dataframe 二维索引经真实事件装配后只使用 row。"""

    app, callback = registered_review_callback
    assert len(callback.inputs) == 2
    assert callback.inputs[0].value == []
    assert callback.inputs[1].value["access_token"] == ""
    monkeypatch.setattr(gradio_app_module, "_authenticated_state", lambda state: state)
    loaders = _StubLoaders()
    _install(loaders)

    _response, state = _run_registered_select(
        app,
        callback,
        items=_items(),
        index=(1, 3),
    )

    assert loaders.calls == [
        (
            "detail",
            {"submission_id": "submission-1", "answer_id": "a-2"},
        )
    ]
    assert state[callback.outputs[8]._id]["answer_id"] == "a-2"


def test_registered_select_keeps_integer_index(
    registered_review_callback: tuple[gr.Blocks, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一维整数索引经真实事件装配后仍选择对应行。"""

    app, callback = registered_review_callback
    monkeypatch.setattr(gradio_app_module, "_authenticated_state", lambda state: state)
    loaders = _StubLoaders()
    _install(loaders)

    _response, state = _run_registered_select(
        app,
        callback,
        items=_items(),
        index=0,
    )

    assert loaders.calls[0][1]["answer_id"] == "answer-1"
    assert state[callback.outputs[8]._id]["answer_id"] == "answer-1"


@pytest.mark.parametrize(
    ("index", "selected", "has_items"),
    [
        (None, True, True),
        ((-1, 0), True, True),
        ((2, 0), True, True),
        ((0, 0), False, True),
        ((0, 0), True, False),
    ],
)
def test_registered_select_returns_empty_state_for_invalid_selection(
    registered_review_callback: tuple[gr.Blocks, Any],
    monkeypatch: pytest.MonkeyPatch,
    index: Any,
    selected: bool,
    has_items: bool,
) -> None:
    """空选择、取消、负值及越界都返回明确空态。"""

    app, callback = registered_review_callback
    monkeypatch.setattr(gradio_app_module, "_authenticated_state", lambda state: state)
    loaders = _StubLoaders()
    _install(loaders)

    response, state = _run_registered_select(
        app,
        callback,
        items=_items() if has_items else [],
        index=index,
        selected=selected,
    )

    assert loaders.calls == []
    assert state[callback.outputs[8]._id] is None
    assert view.NO_SELECTION_MESSAGE in response["data"][11]


def test_select_item_renders_dual_pane_detail() -> None:
    """选中记录后渲染学生答案、AI 评分、题目依据与检索依据。"""

    _install(_StubLoaders())
    render = view.select_review_item(_items(), _TEACHER_STATE, _SelectEvent(0))

    header, answer, score, reason = render[0], render[1], render[2], render[3]
    knowledge_points, reference_answer, rubric, evidence = (
        render[4],
        render[5],
        render[6],
        render[7],
    )
    selected, confirm, save, message = render[8], render[9], render[10], render[11]

    assert "学生甲" in header and "30%" in header
    assert "待人工复核" in header
    assert answer == "变量用于保存数据。"
    assert score == 6.0
    assert reason == "说明了变量的作用。"
    assert "变量" in knowledge_points
    assert "引用数据" in knowledge_points
    assert "变量用于保存数据。" in reference_answer
    assert "说明保存和引用数据即可。" in rubric
    assert "课程资料.pdf" in evidence
    assert "无法解析" in evidence and "已过滤" in evidence
    assert selected is not None and selected["answer_id"] == "answer-1"
    assert _interactive(confirm) is True and _interactive(save) is True
    assert "待人工复核" in message


def test_select_item_without_items_is_empty_state() -> None:
    """没有条目时不返回选中项，操作保持禁用。"""

    _install(_StubLoaders())
    render = view.select_review_item([], _TEACHER_STATE, _SelectEvent(0))

    assert render[8] is None
    assert _interactive(render[9]) is False and _interactive(render[10]) is False
    assert "尚无复核记录" in render[0] or "请先" in render[11]


def test_select_item_disables_actions_when_decided() -> None:
    """已形成教师结论的记录不允许再次提交。"""

    _install(
        _StubLoaders(
            detail_factory=lambda answer_id: _detail(
                review_status=ReviewStatus.CONFIRMED, answer_id=answer_id or "answer-1"
            )
        )
    )
    items = [_queue_item(review_status=ReviewStatus.CONFIRMED).model_dump(mode="json")]
    render = view.select_review_item(items, _TEACHER_STATE, _SelectEvent(0))

    assert _interactive(render[9]) is False and _interactive(render[10]) is False
    assert view.NOT_PENDING_MESSAGE in render[11]


def test_next_item_moves_to_following_record() -> None:
    """下一条切换到队列中的下一条记录。"""

    _install(_StubLoaders())
    render = view.next_review_item(
        {"answer_id": "answer-1"}, _items(), _TEACHER_STATE
    )

    assert render[8] is not None and render[8]["answer_id"] == "a-2"


# ---------------------------------------------------------------- 决策


def test_confirm_submits_and_refreshes_queue() -> None:
    """确认回调提交结论并按最新队列刷新。"""

    loaders = _StubLoaders()
    _install(loaders)

    result = asyncio.run(
        view.confirm_review(
            {"submission_id": "submission-1", "answer_id": "answer-1", "workflow_id": "grading-1"},
            "",
            "",
            "",
            _TEACHER_STATE,
        )
    )

    assert "教师结论已生效" in result[0]
    assert result[1][0][0] == "学生甲"
    assert loaders.calls[0][0] == "decision"
    assert loaders.calls[0][1]["action"] == "confirm"
    assert loaders.calls[0][1]["workflow_id"] == "grading-1"
    assert loaders.calls[0][1]["expected_review_status"] == ReviewStatus.PENDING_REVIEW.value


def test_modify_requires_reason_before_calling_service() -> None:
    """修改缺理由时直接提示，不调用服务。"""

    loaders = _StubLoaders()
    _install(loaders)

    result = asyncio.run(
        view.save_review_changes(
            {"submission_id": "submission-1", "answer_id": "answer-1"},
            8.0,
            "  ",
            "",
            "",
            "",
            _TEACHER_STATE,
        )
    )

    assert "必须填写评分理由" in result[0]
    assert all(call[0] != "decision" for call in loaders.calls)


def test_modify_submits_score_and_reason() -> None:
    """修改回调提交分数与理由，并在成功后刷新队列。"""

    loaders = _StubLoaders()
    _install(loaders)

    result = asyncio.run(
        view.save_review_changes(
            {"submission_id": "submission-1", "answer_id": "answer-1"},
            8.5,
            "补充引用说明。",
            "",
            "",
            "",
            _TEACHER_STATE,
        )
    )

    assert result[0]
    assert loaders.calls[0][1]["action"] == "modify"
    assert loaders.calls[0][1]["score"] == 8.5
    assert loaders.calls[0][1]["reason"] == "补充引用说明。"
    assert loaders.calls[1][0] == "queue"


def test_partial_success_is_reported_honestly() -> None:
    """决定已保存但恢复未完成时如实提示，不伪装成已恢复。"""

    loaders = _StubLoaders(
        outcome=_outcome(resume_status="failed", resume_error_code="REVIEW_SERVICE_CONFLICT")
    )
    _install(loaders)

    result = asyncio.run(
        view.confirm_review(
            {"submission_id": "submission-1", "answer_id": "answer-1"},
            "",
            "",
            "",
            _TEACHER_STATE,
        )
    )

    assert "教师结论已保存" in result[0]
    assert "恢复失败" in result[0]
    assert "REVIEW_SERVICE_CONFLICT" in result[0]


def test_conflict_error_keeps_detail_facts() -> None:
    """陈旧或冲突错误时保留详情事实并给出错误提示。"""

    loaders = _StubLoaders(
        decision_error=ReviewQueryError(
            "该题已形成教师结论。", error_code="REVIEW_DECISION_STALE"
        )
    )
    _install(loaders)

    result = asyncio.run(
        view.confirm_review(
            {"submission_id": "submission-1", "answer_id": "answer-1"},
            "",
            "",
            "",
            _TEACHER_STATE,
        )
    )

    assert "REVIEW_DECISION_STALE" in result[0]
    assert result[1] == []
    assert result[4] == "变量用于保存数据。"


def test_confirm_without_selection_is_rejected() -> None:
    """未选择记录时直接提示，不调用服务。"""

    loaders = _StubLoaders()
    _install(loaders)

    result = asyncio.run(
        view.confirm_review(None, "", "", "", _TEACHER_STATE)
    )

    assert view.NO_SELECTION_MESSAGE in result[0]
    assert all(call[0] != "decision" for call in loaders.calls)


def test_queue_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """读模型未就绪时给出明确错误提示。"""

    loaders = _StubLoaders(
        queue_error=ReviewQueryError(
            "结果存储未就绪。", error_code="REVIEW_QUERY_STORE_NOT_READY"
        )
    )
    _install(loaders)

    rows, items, message = view.refresh_review_queue("", "", "", _TEACHER_STATE)

    assert rows == [] and items == []
    assert "REVIEW_QUERY_STORE_NOT_READY" in message


def test_loader_protocol_exposes_expected_functions() -> None:
    """接线点协议与默认加载器保持稳定（防止视图直接导入端点或会话）。"""

    from backend.app.ui import review_loaders

    for name in ("load_queue", "load_detail", "submit_decision", "require_teacher"):
        assert hasattr(review_loaders, name)
    assert not hasattr(view, "get_session_factory")
    assert not any(
        name in view.__dict__ for name in ("Session", "get_db", "ReviewService")
    )
