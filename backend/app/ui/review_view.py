"""教师阅卷复核工作台：复核队列、双栏详情与教师确认/修改。

S01 约束（评审意见）：本视图只依赖 ``review_loaders`` 提供的加载函数与展示 DTO，
不直接导入 FastAPI 端点、ORM 会话或领域仓储。

边界（不伪造）：

- 队列与详情全部来自持久化事实；没有记录时保持明确空态，不构造示例分数或置信度；
- 检索依据只展示按课程校验通过的片段；无法解析或属于其他课程的引用明确计数；
- 提交后按最新队列刷新；“结论已保存但恢复未完成”如实展示，不伪装成已恢复；
- 本批不提供 ``Re-grade`` 按钮（API 亦不支持），界面不显示无实现的操作。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api.reviews import (
    ReviewDecisionOutcomeDTO,
    ReviewDetailDTO,
    ReviewQueryError,
    ReviewQueuePageDTO,
)
from backend.app.domain.enums import ReviewStatus
from backend.app.domain.permissions import PermissionDeniedError
from backend.app.ui import review_loaders as loaders
from backend.app.ui.layout_view import (
    empty_state,
    feedback,
    status_banner,
    status_label,
)

REVIEW_QUEUE_HEADERS = ("学生", "考试", "题号", "复核状态")
REVIEW_STATUS_CHOICES = (
    ("待人工复核", "Pending Review"),
    ("已确认", "Confirmed"),
    ("已修改", "Modified"),
    ("复核已完成", "Final"),
)

#: 队列为空时的明确空态。
NO_QUEUE_ITEM_MESSAGE = "当前筛选条件下没有可复核的评分记录。"
#: 未选中条目时的提示。
NO_SELECTION_MESSAGE = "请先在复核队列中选择一条记录。"
#: 非待复核状态的说明。
NOT_PENDING_MESSAGE = "该题已形成教师结论，本批不提供再次提交或重评操作。"
#: 检索依据未落库说明。
EVIDENCE_SCOPE_NOTE = "检索依据来自评分时写入的片段标识，按课程二次校验后展示。"


@dataclass(frozen=True)
class ReviewView:
    """复核视图中可由主应用切换的组件集合。"""

    panel: gr.Column
    queue: gr.Dataframe
    answer: gr.Textbox
    score: gr.Textbox
    reason: gr.Textbox
    evidence: gr.Markdown
    message: gr.Markdown
    review_context: gr.State | None = None
    exam_filter: gr.Textbox | None = None
    student_filter: gr.Textbox | None = None
    detail_header: gr.Markdown | None = None
    knowledge_points: gr.Markdown | None = None
    reference_answer: gr.Markdown | None = None
    scoring_rubric: gr.Markdown | None = None
    queue_items: gr.State | None = None
    selected_item: gr.State | None = None
    confirm_button: gr.Button | None = None
    save_button: gr.Button | None = None
    images: gr.HTML | None = None


@dataclass(frozen=True)
class ReviewLoaders:
    """复核视图接线点；默认全部指向生产加载器。"""

    queue: Callable[..., ReviewQueuePageDTO]
    detail: Callable[..., ReviewDetailDTO]
    decision: Callable[..., Any]
    images: Callable[..., str]


_DEFAULT_LOADERS = ReviewLoaders(
    queue=loaders.load_queue,
    detail=loaders.load_detail,
    decision=loaders.submit_decision,
    images=loaders.load_images,
)

_active_loaders: ReviewLoaders = _DEFAULT_LOADERS


def configure_review_loaders(
    *,
    queue: Callable[..., Any] | None = None,
    detail: Callable[..., Any] | None = None,
    decision: Callable[..., Any] | None = None,
    images: Callable[..., str] | None = None,
) -> None:
    """注入复核接线点（传 ``None`` 表示沿用生产默认）。"""

    global _active_loaders
    _active_loaders = ReviewLoaders(
        queue=queue or _DEFAULT_LOADERS.queue,
        detail=detail or _DEFAULT_LOADERS.detail,
        decision=decision or _DEFAULT_LOADERS.decision,
        images=images or _DEFAULT_LOADERS.images,
    )


def _empty_state() -> dict[str, Any]:
    """返回视图使用的空会话状态。"""

    return {"access_token": "", "roles": [], "user_id": "", "username": ""}


def _ensure_teacher(state: Mapping[str, Any] | None) -> None:
    """界面层快速失败：只有已登录教师可以访问复核工作台。"""

    if not isinstance(state, Mapping) or not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    roles = {str(role).strip().lower() for role in state.get("roles", [])}
    if {"teacher", "教师"} & roles == set():
        raise PermissionDeniedError("当前账号无权访问阅卷复核功能。")


def _value(item: Mapping[str, Any] | Any, name: str, default: Any = "") -> Any:
    """从 DTO 或映射中读取字段，缺省时返回默认值。"""

    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _as_dict(item: Mapping[str, Any] | Any) -> dict[str, Any]:
    """把详情 DTO 转换为可放入 ``gr.State`` 的普通字典。"""

    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "model_dump"):
        return dict(item.model_dump(mode="json"))
    return {}


def _error_message(error: BaseException) -> str:
    """把加载器异常转换为明确的中文提示。"""

    if isinstance(error, ReviewQueryError):
        return feedback(f"操作未完成（{error.error_code}）：{error.detail}", "error")
    if isinstance(error, PermissionDeniedError):
        return feedback(str(error), "error")
    if isinstance(error, SQLAlchemyError):
        return feedback("数据库访问失败，请稍后重试。", "error")
    if hasattr(error, "error_code"):
        code = str(error.error_code)
        detail = str(getattr(error, "detail", "") or "")
        return feedback(f"复核服务未完成操作（{code}）：{detail}", "error")
    return feedback("操作未完成：复核链路返回未知错误。", "error")


def _status_text(value: Any) -> str:
    """用平台状态标签展示复核状态；未知值原样展示。"""

    try:
        return status_label(value, entity="review")
    except (KeyError, TypeError, ValueError):
        return str(value or "")


def review_queue_rows(
    page: ReviewQueuePageDTO | Sequence[Mapping[str, Any] | Any] | None,
) -> list[list[str]]:
    """把复核队列转换为表格行；题号缺失时保持明确占位。"""

    items: Sequence[Any]
    if isinstance(page, ReviewQueuePageDTO):
        items = page.items
    elif page is None:
        items = ()
    else:
        items = page
    rows: list[list[str]] = []
    for item in items:
        number = _value(item, "question_number", 0) or 0
        rows.append(
            [
                str(_value(item, "student_name", "")),
                str(_value(item, "exam_title", "")),
                f"第 {number} 题" if number else "题号未知",
                _status_text(_value(item, "review_status", "")),
            ]
        )
    return rows


def _evidence_markdown(item: Mapping[str, Any] | Any | None) -> str:
    """渲染编号检索依据；没有真实依据时显示空态。"""

    if item is None:
        return empty_state("请选择复核记录以查看检索依据。")
    evidence = _value(item, "evidence", []) or []
    fixed = _value(item, "scoring_input", None)
    saved_context = _value(fixed, "course_context", None)
    lines: list[str] = []
    if saved_context is not None:
        lines.extend(["**本轮实际模型检索输入（已保存）：**", str(saved_context)])
        references = _value(fixed, "source_references", []) or []
        lines.append(
            "**实际引用：** "
            + "、".join(str(_value(value, "chunk_id", "")) for value in references)
        )
    elif fixed is not None:
        lines.append(feedback("本条记录未保存实际模型检索正文，保留未知。", "warning"))
    if evidence:
        lines.append("**当前授权资料正文：**")
    if evidence:
        for index, entry in enumerate(evidence, start=1):
            source = str(
                _value(entry, "source_file", "")
                or _value(entry, "document_id", "")
                or "未标注资料"
            )
            chunk_index = _value(entry, "chunk_index", None)
            location = (
                f"片段 {chunk_index}" if chunk_index is not None else "片段序号未知"
            )
            content = str(_value(entry, "content", ""))
            lines.append(f"{index}. **{source}**（{location}）\n   {content}")
    else:
        lines.append(empty_state("该题没有可展示的检索依据。"))
    unresolved = int(_value(item, "unresolved_evidence_count", 0) or 0)
    out_of_course = int(_value(item, "out_of_course_evidence_count", 0) or 0)
    if unresolved:
        lines.append(
            feedback(f"有 {unresolved} 条引用无法解析到片段，未展示正文。", "warning")
        )
    if out_of_course:
        lines.append(
            feedback(f"已过滤 {out_of_course} 条不属于该课程的检索片段。", "warning")
        )
    lines.append(feedback(EVIDENCE_SCOPE_NOTE, "info"))
    return "\n\n".join(lines)


def _scoring_basis_markdown(item: Mapping[str, Any] | Any) -> str:
    fixed = _value(item, "scoring_input", None)
    original = (
        _value(fixed, "source_rubric", None)
        if fixed is not None
        else _value(item, "scoring_rubric", None)
    )
    lines = [f"**原始评分标准：**\n\n{original or '未提供'}"]
    if fixed is None:
        lines.append(
            feedback("历史记录未记录固定本场评分依据，不能从当前题库推算。", "warning")
        )
        return "\n\n".join(lines)
    lines.append(
        f"**本场满分：** {_value(fixed, 'effective_score', '未知')}　**原题满分：** {_value(fixed, 'base_score', '未知')}"
    )
    basis = _value(fixed, "scoring_basis", None)
    points = _value(basis, "points", []) or []
    if points:
        lines.append("**本场已确认评分要点：**")
        lines.extend(
            f"- {_value(point, 'label', '')}：{_value(point, 'confirmed_points', '未知')} 分"
            for point in points
        )
        lines.append("已确认要点采用本场分值，不得再次按原题比例缩放。")
    else:
        lines.append(
            "**客观题规则：** 按本场满分应用既有确定性规则。"
            if _value(basis, "kind") == "objective"
            else "**本场定性标准：** 原标准已核对与本场满分的对应语义，不生成数值要点、不二次缩放。"
        )
    verified = _value(fixed, "verified_image_conditions", None)
    assets = _value(fixed, "assets", []) or []
    if assets:
        if verified is None:
            lines.append(
                feedback(
                    "必要图片条件尚无当前真实人工核对，保留待核对状态。", "warning"
                )
            )
        else:
            check = _value(verified, "check", None)
            lines.append(
                f"**当前图片核对：** {_value(check, 'teacher_id', '未知')}　{_value(check, 'checked_at', '未知')}"
            )
            conditions = _value(check, "confirmed_conditions", []) or []
            lines.extend(
                f"- 图像条件：{_value(condition, 'text', '')}"
                for condition in conditions
            )
            if not conditions:
                lines.append("当前核对未增加条件文字。")
    return "\n\n".join(lines)


def _detail_fields(item: Mapping[str, Any] | Any | None) -> tuple[Any, ...]:
    """渲染详情字段（不含选中状态与按钮）。"""

    if item is None:
        return (
            empty_state("尚无复核记录。"),
            "",
            "",
            "",
            "**知识点：** 暂无",
            "**参考答案：** 暂无",
            "**评分标准：** 暂无",
            _evidence_markdown(None),
        )
    number = _value(item, "question_number", 0) or 0
    confidence = _value(item, "confidence", None)
    confidence_text = (
        f"{float(confidence):.0%}" if isinstance(confidence, (int, float)) else "未知"
    )
    fixed = _value(item, "scoring_input", None)
    header = "\n\n".join(
        [
            "**学生：** {}　**考试：** {}　**题号：** {}　**实际置信度：** {}".format(
                _value(item, "student_name", "—"),
                _value(item, "exam_title", "—"),
                f"第 {number} 题" if number else "题号未知",
                confidence_text,
            ),
            status_banner(_value(item, "review_status", ""), entity="review"),
        ]
    )
    header += f"\n\n**本场满分：** {_value(item, 'max_score', '未知')}"
    header += "\n\n**题目：**\n\n" + str(
        _value(fixed, "question_content", "")
        if fixed is not None
        else _value(item, "question_content", "")
    )
    if fixed is not None:
        options = _value(fixed, "options", None)
        if options:
            header += "\n\n" + "\n\n".join(
                f"{key}. {value}"
                for key, value in (
                    options.items()
                    if isinstance(options, Mapping)
                    else enumerate(options, start=1)
                )
            )
        if not _value(fixed, "order_preserved", True):
            header += "\n\n" + feedback(
                "历史选项原顺序未知，当前保存顺序不代表已恢复原顺序。", "warning"
            )
    if _value(item, "scoring_input_error", None):
        header += "\n\n" + feedback(
            f"本场输入不可用：{_value(item, 'scoring_input_error')}，请先核对。",
            "error",
        )
    knowledge_points = (
        _value(fixed, "published_knowledge_points", [])
        if fixed is not None
        else _value(item, "knowledge_points", [])
    )
    knowledge_points = knowledge_points or []
    missing = _value(item, "missing_knowledge_points", []) or []
    correct = _value(item, "correct_points", []) or []
    points_text = "、".join(str(point) for point in knowledge_points) or "未标注"
    if missing:
        points_text += f"　**缺失：** {'、'.join(str(point) for point in missing)}"
    if correct:
        points_text += f"　**答对：** {'、'.join(str(point) for point in correct)}"
    score = _value(item, "score", None)
    header += f"\n\n**当前评分：** {escape(str(score)) if score is not None else '未知'}\n\n**已保存评分理由：** {escape(str(_value(item, 'reason', '') or '未提供'))}"
    return (
        header,
        str(_value(item, "student_answer", "")),
        str(score) if score is not None else "",
        str(_value(item, "reason", "") or ""),
        f"**知识点：** {points_text}",
        f"**参考答案：**\n\n{(_value(fixed, 'reference_answer', None) if fixed is not None else _value(item, 'reference_answer', None)) or '未提供'}",
        _scoring_basis_markdown(item),
        _evidence_markdown(item),
    )


def _action_updates(item: Mapping[str, Any] | Any | None) -> tuple[Any, Any]:
    """按权威复核状态决定确认/保存按钮是否可用。"""

    if item is None or _value(item, "scoring_input_error", None):
        return gr.update(interactive=False), gr.update(interactive=False)
    status = str(_value(item, "review_status", ""))
    if status in {ReviewStatus.PENDING_REVIEW.value, ReviewStatus.PENDING_REVIEW.name}:
        return gr.update(interactive=True), gr.update(interactive=True)
    return gr.update(interactive=False), gr.update(interactive=False)


def _detail_render(
    item: Mapping[str, Any] | Any | None,
    message: str,
) -> tuple[Any, ...]:
    """按组件顺序渲染详情：字段 + 选中状态 + 按钮 + 消息（共 12 项）。"""

    confirm, save = _action_updates(item)
    return (
        *_detail_fields(item),
        _as_dict(item) if item is not None else None,
        confirm,
        save,
        message,
    )


def refresh_review_queue(
    exam_filter: str = "",
    student_filter: str = "",
    review_status: Any = ReviewStatus.PENDING_REVIEW.value,
    state: Mapping[str, Any] | None = None,
) -> tuple[list[list[str]], list[dict[str, Any]], str]:
    """刷新复核队列；没有记录时给出明确空态。"""

    try:
        _ensure_teacher(state)
        page = _active_loaders.queue(
            state,
            exam_id=str(exam_filter or "").strip() or None,
            student_id=str(student_filter or "").strip() or None,
            review_status=str(review_status or "").strip() or None,
        )
    except (
        PermissionDeniedError,
        ReviewQueryError,
        SQLAlchemyError,
    ) as error:
        return [], [], _error_message(error)
    items = [_as_dict(item) for item in page.items]
    if not items:
        return [], [], feedback(NO_QUEUE_ITEM_MESSAGE, "info")
    return (
        review_queue_rows(page),
        items,
        feedback(f"共 {page.total} 条复核记录，本次展示 {len(items)} 条。", "info"),
    )


def _load_detail(
    item: Mapping[str, Any] | Any | None,
    state: Mapping[str, Any] | None,
) -> Any:
    """按队列条目读取权威详情；失败时不构造任何展示事实。"""

    if item is None:
        return None
    submission_id = str(_value(item, "submission_id", ""))
    answer_id = str(_value(item, "answer_id", ""))
    if not submission_id or not answer_id:
        return None
    return _active_loaders.detail(
        state, submission_id=submission_id, answer_id=answer_id
    )


def load_selected_review_images(
    selected: Mapping[str, Any] | None, state: Mapping[str, Any] | None = None
) -> str:
    """只回传记录身份，原图来自重新授权后的详情；失败清空旧图。"""
    if not selected:
        return "<p>请先选择复核记录以查看本场原图。</p>"
    try:
        _ensure_teacher(state)
        return _active_loaders.images(
            state,
            submission_id=str(selected.get("submission_id") or ""),
            answer_id=str(selected.get("answer_id") or ""),
        )
    except (PermissionDeniedError, ReviewQueryError, SQLAlchemyError) as error:
        return f'<p role="alert">{escape(_error_message(error))}</p>'


def select_review_item(
    queue_items: Sequence[Mapping[str, Any]] | None,
    state: Mapping[str, Any] | None,
    evt: gr.SelectData,
) -> tuple[Any, ...]:
    """选中队列记录：加载权威详情并按状态启用或禁用操作。"""

    items = list(queue_items or [])
    raw_index = getattr(evt, "index", None)
    if isinstance(raw_index, (tuple, list)):
        raw_index = raw_index[0] if raw_index else None
    index = (
        raw_index
        if isinstance(raw_index, int) and not isinstance(raw_index, bool)
        else None
    )
    try:
        _ensure_teacher(state)
    except PermissionDeniedError as error:
        return _detail_render(None, _error_message(error))
    if (
        not getattr(evt, "selected", True)
        or index is None
        or index < 0
        or index >= len(items)
    ):
        return _detail_render(None, feedback(NO_SELECTION_MESSAGE, "warning"))
    return _render_detail_for(items[index], state)


def _render_detail_for(
    queue_item: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    *,
    selected_message: str | None = None,
) -> tuple[Any, ...]:
    """读取详情并渲染；详情不可用时保持明确错误提示。"""

    try:
        detail = _load_detail(queue_item, state)
    except (
        PermissionDeniedError,
        ReviewQueryError,
        SQLAlchemyError,
    ) as error:
        return _detail_render(None, _error_message(error))
    if detail is None:
        return _detail_render(None, feedback(NO_SELECTION_MESSAGE, "warning"))
    item = _as_dict(detail)
    status = str(_value(item, "review_status", ""))
    if selected_message is not None:
        message = feedback(selected_message, "info")
    elif status in {
        ReviewStatus.PENDING_REVIEW.value,
        ReviewStatus.PENDING_REVIEW.name,
    }:
        message = feedback("该题待人工复核：可确认 AI 评分或修改分数与理由。", "info")
    else:
        message = feedback(NOT_PENDING_MESSAGE, "warning")
    return _detail_render(item, message)


def cancel_review_edit(selected, state):
    """Discard local inputs and re-read current authorized review without a decision."""
    try:
        _ensure_teacher(state)
    except PermissionDeniedError as error:
        return _detail_render(None, _error_message(error))
    if not selected:
        return _detail_render(None, feedback(NO_SELECTION_MESSAGE, "warning"))
    return _render_detail_for(
        selected, state, selected_message="已取消修改，重新读取当前评分。"
    )


def next_review_item(
    selected: Mapping[str, Any] | None,
    queue_items: Sequence[Mapping[str, Any]] | None,
    state: Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """切换到队列中的下一条记录；已到最后一条时保持当前条目。"""

    items = list(queue_items or [])
    if not items:
        return _detail_render(None, feedback(NO_QUEUE_ITEM_MESSAGE, "info"))
    current_id = str((selected or {}).get("answer_id") or "")
    index = 0
    for position, item in enumerate(items):
        if str(_value(item, "answer_id", "")) == current_id:
            index = min(position + 1, len(items) - 1)
            break
    return _render_detail_for(
        items[index], state, selected_message="已切换到下一条复核记录。"
    )


async def confirm_review(
    selected: Mapping[str, Any] | None,
    exam_filter: str,
    student_filter: str,
    review_status: Any,
    state: Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """确认 AI 评分并刷新队列；部分成功如实展示。"""

    return await _submit_review_action(
        "confirm",
        selected,
        score=None,
        reason=None,
        exam_filter=exam_filter,
        student_filter=student_filter,
        review_status=review_status,
        state=state,
    )


async def save_review_changes(
    selected: Mapping[str, Any] | None,
    score: Any,
    reason: str | None,
    exam_filter: str,
    student_filter: str,
    review_status: Any,
    state: Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """保存修改后的分数与理由并刷新队列。"""

    return await _submit_review_action(
        "modify",
        selected,
        score=score,
        reason=reason,
        exam_filter=exam_filter,
        student_filter=student_filter,
        review_status=review_status,
        state=state,
    )


async def _submit_review_action(
    action: str,
    selected: Mapping[str, Any] | None,
    *,
    score: Any,
    reason: str | None,
    exam_filter: str,
    student_filter: str,
    review_status: Any,
    state: Mapping[str, Any] | None,
) -> tuple[Any, ...]:
    """提交教师决策并按最新队列刷新；失败时保留既有选择与详情。"""

    item = selected or None
    if not item or not str(item.get("answer_id") or ""):
        return _action_outputs(
            feedback(NO_SELECTION_MESSAGE, "warning"),
            ([], [], feedback(NO_QUEUE_ITEM_MESSAGE, "info")),
            _detail_render(None, feedback(NO_SELECTION_MESSAGE, "warning")),
        )
    if action == "modify" and not str(reason or "").strip():
        message = feedback("保存修改必须填写评分理由。", "warning")
        return _action_outputs(
            message,
            ([], [], feedback(NO_QUEUE_ITEM_MESSAGE, "info")),
            _render_detail_for(item, state, selected_message=message),
        )
    try:
        outcome = await _active_loaders.decision(
            state,
            submission_id=str(item.get("submission_id") or ""),
            answer_id=str(item.get("answer_id") or ""),
            action=action,
            score=score,
            reason=reason,
            workflow_id=str(item.get("workflow_id") or "") or None,
            expected_review_status=ReviewStatus.PENDING_REVIEW.value,
            expected_review_round_id=str(item.get("pending_review_round_id") or "")
            or None,
        )
    except (
        PermissionDeniedError,
        ReviewQueryError,
        SQLAlchemyError,
    ) as error:
        message = _error_message(error)
        return _action_outputs(
            message,
            ([], [], feedback(NO_QUEUE_ITEM_MESSAGE, "info")),
            _render_detail_for(item, state, selected_message=message),
        )
    assert isinstance(outcome, ReviewDecisionOutcomeDTO)
    message = _outcome_markdown(outcome)
    rows, items, list_message = refresh_review_queue(
        exam_filter, student_filter, review_status, state
    )
    detail = _render_detail_for(item, state, selected_message=message)
    return _action_outputs(message, (rows, items, list_message), detail)


def _outcome_markdown(outcome: ReviewDecisionOutcomeDTO) -> str:
    """把决策回执转换为界面提示，明确区分已恢复与待继续。"""

    label = _status_text(outcome.decision)
    if outcome.resume_status == "succeeded":
        return feedback(f"教师结论已生效（{label}），原工作流已恢复。", "success")
    if outcome.resume_status == "failed":
        return feedback(
            f"教师结论已保存（{label}），但恢复失败"
            f"（{outcome.resume_error_code or '未知原因'}）；请稍后重试恢复。",
            "error",
        )
    return feedback(
        f"教师结论已保存（{label}），流程待继续恢复；可稍后重试恢复，无需重复提交结论。",
        "warning",
    )


def _action_outputs(
    message: str,
    queue_result: tuple[list[list[str]], list[dict[str, Any]], str],
    detail: tuple[Any, ...],
) -> tuple[Any, ...]:
    """组合动作输出：消息 + 队列事实 + 详情（含选中状态与按钮）。"""

    rows, items, _ = queue_result
    return (message, rows, items, *detail[:8], detail[8], detail[9], detail[10])


def create_review_view(session_state: Any | None = None) -> ReviewView:
    """创建教师阅卷复核工作台。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(
        visible=False,
        elem_id="edu-review",
        elem_classes=["edu-review", "edu-business-page"],
    ) as panel:
        gr.HTML(
            "<style>.edu-review .review-actions {position:sticky;bottom:0;"
            "z-index:5;background:var(--background-fill-primary);padding:12px 0;}"
            ".edu-review .review-queue-column {flex:0 0 240px !important;}"
            ".edu-review .review-actions button {min-height:44px;}"
            "@media(max-width:767px){.edu-review .review-queue-column "
            "{flex:1 1 100% !important;}}</style>"
        )
        gr.Markdown("## 阅卷复核")
        review_context = gr.State(None)
        with gr.Row(elem_classes=["review-filters"]):
            exam_filter = gr.Textbox(label="考试", placeholder="按考试筛选")
            student_filter = gr.Textbox(label="学生", placeholder="按学生筛选")
            status_filter = gr.Dropdown(
                choices=list(REVIEW_STATUS_CHOICES),
                label="复核状态",
                value=ReviewStatus.PENDING_REVIEW.value,
            )
            refresh = gr.Button(
                "刷新队列",
                variant="primary",
                elem_id="edu-review-refresh",
                elem_classes="edu-icon",
            )
        message = gr.Markdown(empty_state("暂无可复核的评分记录"))
        with gr.Row(equal_height=False):
            with gr.Column(scale=0, min_width=240, elem_classes="review-queue-column"):
                gr.Markdown("### 复核队列")
                queue = gr.Dataframe(
                    headers=list(REVIEW_QUEUE_HEADERS),
                    datatype=["str"] * len(REVIEW_QUEUE_HEADERS),
                    value=[],
                    interactive=False,
                    label="待复核评分",
                    elem_classes=["review-queue"],
                )
                queue_items = gr.State([])
                selected_item = gr.State(None)
            with gr.Column(scale=1, min_width=0):
                detail_header = gr.Markdown(empty_state("尚未选择复核记录。"))
                with gr.Row():
                    with gr.Column(scale=45):
                        answer = gr.Textbox(
                            label="学生答案（原文）",
                            lines=14,
                            interactive=False,
                        )
                    with gr.Column(scale=55):
                        with gr.Accordion(
                            "修改评分", open=False, elem_id="edu-review-editor"
                        ) as score_editor:
                            score = gr.Textbox(
                                label="分数（十进制）",
                                value="",
                                interactive=True,
                                placeholder="例如 6.675，由服务按规则校验并舍入",
                            )
                            reason = gr.Textbox(
                                label="评分理由", lines=5, interactive=True
                            )
                            save = gr.Button(
                                "保存修改",
                                interactive=False,
                                elem_id="edu-review-save",
                                elem_classes="edu-icon",
                            )
                            cancel = gr.Button(
                                "取消评分修改",
                                elem_id="edu-review-cancel",
                                elem_classes="edu-icon",
                            )
                        knowledge_points = gr.Markdown("**知识点：** 暂无")
                        reference_answer = gr.Markdown("**参考答案：** 暂无")
                        scoring_rubric = gr.Markdown("**评分标准：** 暂无")
                with gr.Accordion("本场原图（教师授权预览）", open=True):
                    images = gr.HTML("<p>尚未选择复核记录。</p>")
                with gr.Accordion("检索依据", open=False):
                    evidence = gr.Markdown(empty_state("暂无检索依据可展示"))
                with gr.Row(elem_classes=["review-actions"]):
                    confirm = gr.Button(
                        "确认评分",
                        variant="primary",
                        interactive=False,
                        elem_id="edu-review-confirm",
                        elem_classes="edu-icon",
                    )
                    next_item = gr.Button(
                        "下一条",
                        interactive=False,
                        elem_id="edu-review-next",
                        elem_classes="edu-icon",
                    )

        refresh.click(
            fn=refresh_review_queue,
            inputs=[exam_filter, student_filter, status_filter, state],
            outputs=[queue, queue_items, message],
            show_progress="hidden",
        )
        queue.select(
            fn=select_review_item,
            inputs=[queue_items, state],
            outputs=[
                detail_header,
                answer,
                score,
                reason,
                knowledge_points,
                reference_answer,
                scoring_rubric,
                evidence,
                selected_item,
                confirm,
                save,
                message,
            ],
            show_progress="hidden",
        ).then(
            fn=load_selected_review_images,
            inputs=[selected_item, state],
            outputs=[images],
            show_progress="hidden",
        )
        cancel.click(
            cancel_review_edit,
            inputs=[selected_item, state],
            outputs=[
                detail_header,
                answer,
                score,
                reason,
                knowledge_points,
                reference_answer,
                scoring_rubric,
                evidence,
                selected_item,
                confirm,
                save,
                message,
            ],
            show_progress="hidden",
        ).then(
            lambda: gr.update(open=False),
            outputs=[score_editor],
            show_progress="hidden",
        )
        _action_outputs_list = [
            message,
            queue,
            queue_items,
            detail_header,
            answer,
            score,
            reason,
            knowledge_points,
            reference_answer,
            scoring_rubric,
            evidence,
            selected_item,
            confirm,
            save,
        ]
        confirm.click(
            fn=confirm_review,
            inputs=[selected_item, exam_filter, student_filter, status_filter, state],
            outputs=_action_outputs_list,
            show_progress="hidden",
        ).then(
            fn=load_selected_review_images,
            inputs=[selected_item, state],
            outputs=[images],
            show_progress="hidden",
        )
        save.click(
            fn=save_review_changes,
            inputs=[
                selected_item,
                score,
                reason,
                exam_filter,
                student_filter,
                status_filter,
                state,
            ],
            outputs=_action_outputs_list,
            show_progress="hidden",
        ).then(
            fn=load_selected_review_images,
            inputs=[selected_item, state],
            outputs=[images],
            show_progress="hidden",
        )
        next_item.click(
            fn=next_review_item,
            inputs=[selected_item, queue_items, state],
            outputs=[
                detail_header,
                answer,
                score,
                reason,
                knowledge_points,
                reference_answer,
                scoring_rubric,
                evidence,
                selected_item,
                confirm,
                save,
                message,
            ],
            show_progress="hidden",
        ).then(
            fn=load_selected_review_images,
            inputs=[selected_item, state],
            outputs=[images],
            show_progress="hidden",
        )

    return ReviewView(
        panel=panel,
        queue=queue,
        answer=answer,
        score=score,
        reason=reason,
        evidence=evidence,
        message=message,
        review_context=review_context,
        exam_filter=exam_filter,
        student_filter=student_filter,
        detail_header=detail_header,
        knowledge_points=knowledge_points,
        reference_answer=reference_answer,
        scoring_rubric=scoring_rubric,
        queue_items=queue_items,
        selected_item=selected_item,
        confirm_button=confirm,
        save_button=save,
        images=images,
    )


build_review_view = create_review_view

__all__ = [
    "EVIDENCE_SCOPE_NOTE",
    "NOT_PENDING_MESSAGE",
    "NO_QUEUE_ITEM_MESSAGE",
    "NO_SELECTION_MESSAGE",
    "REVIEW_QUEUE_HEADERS",
    "REVIEW_STATUS_CHOICES",
    "ReviewLoaders",
    "ReviewView",
    "build_review_view",
    "configure_review_loaders",
    "confirm_review",
    "create_review_view",
    "load_selected_review_images",
    "next_review_item",
    "refresh_review_queue",
    "review_queue_rows",
    "save_review_changes",
    "select_review_item",
]
