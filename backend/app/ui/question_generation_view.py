"""教师 AI 出题视图：条件表单、候选题列表、教师审核与检索依据。

S01 约束（评审意见）：本视图只依赖 ``question_generation_loaders`` 提供的加载函数与展示
DTO，不直接导入 FastAPI 端点、ORM 会话或领域仓储；所有业务状态以服务返回的事实为准。

边界（不伪造）：

- 生成失败、检索上下文不足、结构校验未通过都显示明确提示，绝不生成示例候选题或虚构引用；
- “审核通过/退回修订”只在候选题确实处于 ``Pending Review`` 时可用；按钮禁用只改善体验，
  服务端仍会重新执行权限、状态与结构校验；
- 退回修订意见在题库模型中没有持久列，界面提示“本次提交意见已随响应回显，未落库”。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api.question_generation import (
    CANDIDATE_STATUS_SCOPE,
    CandidateDTO,
    CandidateGenerationResponse,
    CandidatePageDTO,
    CandidateReviewOutcomeDTO,
    QuestionGenerationError,
)
from backend.app.domain.enums import QuestionStatus, QuestionType, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.services.course_service import CourseServiceError
from backend.app.ui import question_generation_loaders as loaders
from backend.app.ui.layout_view import (
    empty_state,
    feedback,
    status_choices,
    status_label,
)

#: 出题条件表头（左栏条件摘要）。
CONDITION_HEADERS = ("条件", "当前值")
#: 候选题表头；状态列展示题库中的权威审核状态。
CANDIDATE_HEADERS = ("候选题目", "题型", "状态", "分值", "知识点")

#: 出题链路未就绪（Provider/Embedding/检索存储）时的统一提示。
GENERATION_UNAVAILABLE_MESSAGE = (
    "AI 出题链路暂不可用：Provider、Embedding 或检索存储未就绪，请联系管理员检查配置。"
)
#: 检索上下文不足时的提示（FR-026：不允许静默编造）。
INSUFFICIENT_CONTEXT_MESSAGE = (
    "检索上下文不足：没有获得足够的课程依据，请调整知识点或补充知识库资料后重试。"
)
#: 候选列表为空时的明确空态。
NO_CANDIDATE_MESSAGE = "暂无候选题目：请先填写条件并生成，或切换到包含候选题的课程。"
#: 审核通过前的状态门禁说明。
NOT_PENDING_REVIEW_MESSAGE = "只有处于“待审核”状态的候选题才能执行审核通过与退回修订。"
#: 修订意见的落库事实说明。
COMMENT_NOT_PERSISTED_NOTE = "修订意见已在本次响应中回显；题库模型暂无该列，因此未落库。"

#: 未就绪错误码集合：命中时显示统一的链路未就绪提示。
_NOT_READY_CODES = frozenset(
    {
        "QUESTION_PROVIDER_NOT_READY",
        "QUESTION_EMBEDDING_PROVIDER_NOT_READY",
        "QUESTION_EMBEDDING_PROVIDER_FAILED",
        "QUESTION_RETRIEVAL_UNSUPPORTED_DIALECT",
        "QUESTION_RETRIEVAL_DATABASE_FAILED",
        "QUESTION_CANDIDATE_STORE_NOT_READY",
    }
)


@dataclass(frozen=True)
class QuestionGenerationView:
    """AI 出题面板中由主工作台控制的组件。"""

    panel: gr.Column
    candidates_table: gr.Dataframe
    message: gr.Markdown
    candidate_preview: gr.Markdown | None = None
    evidence: gr.Markdown | None = None
    generation_state: gr.Markdown | None = None
    selected_candidate: gr.State | None = None
    candidate_rows: gr.State | None = None
    approve_button: gr.Button | None = None
    revision_button: gr.Button | None = None


@dataclass(frozen=True)
class QuestionGenerationLoaders:
    """出题视图的接线点；默认全部指向生产加载器。"""

    courses: Callable[[Mapping[str, Any] | None], list[tuple[str, str]]]
    generate: Callable[..., Any]
    list_candidates: Callable[..., CandidatePageDTO]
    candidate_detail: Callable[..., CandidateDTO]
    review: Callable[..., CandidateReviewOutcomeDTO]


_DEFAULT_LOADERS = QuestionGenerationLoaders(
    courses=loaders.load_courses,
    generate=loaders.generate_candidate_batch,
    list_candidates=loaders.list_candidates,
    candidate_detail=loaders.load_candidate,
    review=loaders.submit_candidate_review,
)

_active_loaders: QuestionGenerationLoaders = _DEFAULT_LOADERS


def configure_question_generation_loaders(
    *,
    courses: Callable[..., Any] | None = None,
    generate: Callable[..., Any] | None = None,
    list_candidates: Callable[..., Any] | None = None,
    candidate_detail: Callable[..., Any] | None = None,
    review: Callable[..., Any] | None = None,
) -> None:
    """注入出题接线点（传 ``None`` 表示沿用生产默认）。

    与 ``results_view.configure_results_loaders`` 同构：测试注入替身，应用使用生产加载器。
    """

    global _active_loaders
    _active_loaders = QuestionGenerationLoaders(
        courses=courses or _DEFAULT_LOADERS.courses,
        generate=generate or _DEFAULT_LOADERS.generate,
        list_candidates=list_candidates or _DEFAULT_LOADERS.list_candidates,
        candidate_detail=candidate_detail or _DEFAULT_LOADERS.candidate_detail,
        review=review or _DEFAULT_LOADERS.review,
    )


def _empty_state() -> dict[str, Any]:
    """返回独立视图使用的空会话状态。"""

    return {"access_token": "", "roles": [], "user_id": "", "username": ""}


def _ensure_teacher(state: Mapping[str, Any] | None) -> None:
    """界面层快速失败：只有已登录教师可以访问出题面板。

    这只是体验层的门禁；服务端每次请求都会重新执行权限与课程归属校验。
    """

    if not isinstance(state, Mapping) or not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问 AI 出题功能。") from error
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问 AI 出题功能。")
    if not str(state.get("user_id") or "").strip():
        raise PermissionDeniedError("登录状态缺少用户标识。")


def _value(item: Mapping[str, Any] | Any, name: str, default: Any = "") -> Any:
    """从 DTO 或映射中读取字段，缺省时返回默认值。"""

    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _as_dict(item: Mapping[str, Any] | Any) -> dict[str, Any]:
    """把候选题 DTO 转换为可放入 ``gr.State`` 的普通字典。"""

    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "model_dump"):
        return dict(item.model_dump(mode="json"))
    return {}


def _error_message(error: BaseException) -> str:
    """把加载器异常转换为明确的中文提示，不泄露内部细节。"""

    if isinstance(error, QuestionGenerationError):
        if error.error_code == "QUESTION_INSUFFICIENT_CONTEXT":
            return feedback(INSUFFICIENT_CONTEXT_MESSAGE, "warning")
        if error.error_code in _NOT_READY_CODES:
            return feedback(f"{GENERATION_UNAVAILABLE_MESSAGE}（{error.error_code}）", "error")
        return feedback(f"操作未完成（{error.error_code}）：{error.detail}", "error")
    if isinstance(error, PermissionDeniedError):
        return feedback(str(error), "error")
    if isinstance(error, CourseServiceError):
        return feedback(str(error), "error")
    if isinstance(error, SQLAlchemyError):
        return feedback("数据库访问失败，请稍后重试。", "error")
    return feedback("操作未完成：出题链路返回未知错误。", "error")


def _status_text(value: Any) -> str:
    """用平台状态标签展示题库审核状态；未知值原样展示。"""

    try:
        return status_label(value, entity="question")
    except (KeyError, TypeError, ValueError):
        return str(value or "")


def candidate_rows(page: CandidatePageDTO | Sequence[Any] | None) -> list[list[str]]:
    """把候选题页面转换为表格行；状态列只展示权威审核状态。"""

    items: Sequence[Any]
    if isinstance(page, CandidatePageDTO):
        items = page.items
    elif page is None:
        items = ()
    else:
        items = page
    rows: list[list[str]] = []
    for item in items:
        knowledge_points = _value(item, "knowledge_points", []) or []
        rows.append(
            [
                str(_value(item, "content", "")),
                str(_value(item, "question_type", "")),
                _status_text(_value(item, "status", "")),
                str(_value(item, "score", "")),
                "、".join(str(point) for point in knowledge_points),
            ]
        )
    return rows


def candidate_preview_markdown(item: Mapping[str, Any] | Any | None) -> str:
    """渲染单条候选题完整预览；无选中项时保持明确空态。"""

    if item is None:
        return empty_state("尚未选择候选题。")
    content = str(_value(item, "content", "")).strip()
    if not content:
        return empty_state("尚未选择候选题。")
    status = _value(item, "status", "")
    lines = [
        f"### {content}",
        (
            f"**题型：** {_value(item, 'question_type', '')}　"
            f"**分值：** {_value(item, 'score', '')}　"
            f"**状态：** {_status_text(status)}"
        ),
    ]
    difficulty = str(_value(item, "difficulty", "") or "").strip()
    knowledge_points = _value(item, "knowledge_points", []) or []
    lines.append(
        f"**难度：** {difficulty or '未标注'}　"
        f"**知识点：** {'、'.join(str(point) for point in knowledge_points) or '未标注'}"
    )
    options = _value(item, "options", None)
    if options:
        rendered = "\n".join(f"- {option}" for option in options)
        lines.append(f"**选项**\n{rendered}")
    reference_answer = str(_value(item, "reference_answer", "") or "").strip()
    lines.append(f"**参考答案：** {reference_answer or '未提供'}")
    rubric = str(_value(item, "scoring_rubric", "") or "").strip()
    lines.append(f"**评分标准：** {rubric or '未提供'}")
    return "\n\n".join(lines)


def _evidence_markdown(
    evidence: Sequence[Mapping[str, Any] | Any] | None,
    *,
    out_of_course: int = 0,
    unresolved: int = 0,
    sources_persisted: bool = False,
) -> str:
    """渲染本次生成的检索依据；没有真实依据时显示空态。"""

    lines: list[str] = []
    if evidence:
        for index, item in enumerate(evidence, start=1):
            source = str(
                _value(item, "source_file", "")
                or _value(item, "document_id", "")
                or "未标注资料"
            )
            chunk_index = _value(item, "chunk_index", None)
            location = (
                f"片段 {chunk_index}"
                if chunk_index is not None
                else str(_value(item, "course_id", "") or "未标注课程")
            )
            content = str(_value(item, "content", ""))
            lines.append(f"{index}. **{source}**（{location}）\n   {content}")
    else:
        lines.append(empty_state("本次生成没有可展示的检索依据。"))
    if out_of_course:
        lines.append(
            feedback(f"已过滤 {out_of_course} 条不属于当前课程的检索片段。", "warning")
        )
    if unresolved:
        lines.append(
            feedback(
                f"有 {unresolved} 条引用无法按当前课程解析到片段，未展示其正文。",
                "warning",
            )
        )
    if sources_persisted:
        lines.append(feedback("检索依据已随候选题落库。", "info"))
    else:
        lines.append(
            feedback(
                "候选题的检索依据当前只随生成响应返回，未落库；列表中不展示历史依据。",
                "info",
            )
        )
    return "\n\n".join(lines)


def _parse_question_type(value: Any) -> QuestionType | None:
    """把界面选择的题型值转换为题型枚举；空值返回 ``None``。"""

    text = str(value or "").strip()
    if not text:
        return None
    for candidate in QuestionType:
        if text.lower() in {candidate.value.lower(), candidate.name.lower()}:
            return candidate
    return None


def _parse_status(value: Any) -> QuestionStatus | None:
    """把界面选择的状态值转换为审核状态；空值表示候选状态全范围。"""

    text = str(value or "").strip()
    if not text:
        return None
    for candidate in CANDIDATE_STATUS_SCOPE:
        if text.lower() in {candidate.value.lower(), candidate.name.lower()}:
            return candidate
    return None


def _course_choices(state: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """读取真实课程上下文；课程为空时不创建占位数据。"""

    _ensure_teacher(state)
    return _active_loaders.courses(state)


def refresh_generation_context(state: Mapping[str, Any]) -> tuple[Any, str]:
    """刷新当前课程下拉框与生成状态提示。"""

    try:
        choices = _course_choices(state)
        message = (
            feedback("请选择课程、填写条件后生成候选题目。", "info")
            if choices
            else feedback("当前教师暂无课程，请先创建课程并绑定知识库。", "warning")
        )
        return (
            gr.update(choices=choices, value=choices[0][1] if choices else None),
            message,
        )
    except (PermissionDeniedError, CourseServiceError, SQLAlchemyError) as error:
        return gr.update(choices=[], value=None), _error_message(error)


def show_course_context(course_id: str | None, state: Mapping[str, Any]) -> str:
    """显示当前课程名称；没有课程时保持明确空态。"""

    try:
        choices = _course_choices(state)
    except (PermissionDeniedError, CourseServiceError, SQLAlchemyError) as error:
        return _error_message(error)
    name = next((name for name, identifier in choices if identifier == course_id), None)
    return f"### 当前课程：{name}" if name else empty_state("尚未选择课程。")


def refresh_candidate_list(
    course_id: str | None,
    candidate_status: Any = "",
    state: Mapping[str, Any] | None = None,
) -> tuple[list[list[str]], list[dict[str, Any]], str]:
    """刷新候选题列表；没有候选题时给出明确空态。"""

    if not str(course_id or "").strip():
        if not isinstance(state, Mapping) or not state.get("access_token"):
            return [], [], _error_message(PermissionDeniedError("请先登录。"))
        return [], [], feedback("请先选择课程。", "warning")
    try:
        _ensure_teacher(state)
        page = _active_loaders.list_candidates(
            state,
            course_id=str(course_id),
            candidate_status=_parse_status(candidate_status),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        QuestionGenerationError,
        SQLAlchemyError,
    ) as error:
        return [], [], _error_message(error)
    items = [_as_dict(item) for item in page.items]
    if not items:
        return [], [], feedback(NO_CANDIDATE_MESSAGE, "info")
    return (
        candidate_rows(page),
        items,
        feedback(f"共 {page.total} 条候选题，本次展示 {len(items)} 条。", "info"),
    )


async def generate_candidates(
    course_id: str | None,
    knowledge_point: str | None,
    difficulty: str | None,
    question_type: Any,
    amount: Any,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], list[dict[str, Any]], str, str, Any, Any, str, str]:
    """按条件生成候选题并刷新列表、预览、审核按钮与检索依据。"""

    if not str(course_id or "").strip():
        empty = feedback("请先选择课程后再生成。", "warning")
        return [], [], empty, empty_state("尚未选择候选题。"), *_disabled_actions(), "", empty
    points = [point.strip() for point in str(knowledge_point or "").split(",") if point.strip()]
    try:
        count = int(amount or 1)
    except (TypeError, ValueError):
        count = 1
    try:
        _ensure_teacher(state)
        response = await _active_loaders.generate(
            state,
            course_id=str(course_id),
            knowledge_points=points,
            difficulty=difficulty,
            question_type=_parse_question_type(question_type),
            count=max(1, count),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        QuestionGenerationError,
        SQLAlchemyError,
    ) as error:
        message = _error_message(error)
        return [], [], message, empty_state("尚未选择候选题。"), *_disabled_actions(), "", message
    assert isinstance(response, CandidateGenerationResponse)
    items = [_as_dict(candidate) for candidate in response.candidates]
    status_message = _generation_status_markdown(response)
    evidence = _evidence_markdown(
        [_as_dict(item) for item in response.evidence],
        out_of_course=response.out_of_course_source_count,
        unresolved=response.unresolved_source_count,
        sources_persisted=response.sources_persisted,
    )
    rows = candidate_rows(response.candidates)
    preview = candidate_preview_markdown(items[0]) if items else empty_state(NO_CANDIDATE_MESSAGE)
    return rows, items, status_message, preview, *_action_updates(items, 0), evidence, status_message


def _generation_status_markdown(response: CandidateGenerationResponse) -> str:
    """生成完成后展示整批校验结论与待审核数量。"""

    pending = sum(
        1 for item in response.candidates if item.status is QuestionStatus.PENDING_REVIEW
    )
    needs_revision = sum(
        1 for item in response.candidates if item.status is QuestionStatus.NEEDS_REVISION
    )
    parts = [
        (
            f"本次生成 {response.generated_count} 道候选题：待审核 {pending} 道，"
            f"待修订 {needs_revision} 道。"
        )
    ]
    issues = response.batch_validation.issues
    if issues:
        rendered = "；".join(f"{issue.code}：{issue.message}" for issue in issues)
        parts.append(f"整批校验未通过：{rendered}")
    if needs_revision:
        parts.append("存在待修订候选题，请退回修订或重新生成后再审核通过。")
    kind = "success" if pending and not needs_revision else "warning"
    return feedback(" ".join(parts), kind)


def _disabled_actions() -> tuple[Any, Any]:
    """返回两个禁用状态的按钮更新。"""

    return gr.update(interactive=False), gr.update(interactive=False)


def _action_updates(
    items: Sequence[Mapping[str, Any]],
    index: int,
) -> tuple[Any, Any]:
    """按选中候选题的权威状态决定审核按钮是否可用。"""

    if not items or index >= len(items):
        return _disabled_actions()
    status = str(_value(items[index], "status", ""))
    if status not in {QuestionStatus.PENDING_REVIEW.value, QuestionStatus.PENDING_REVIEW.name}:
        return _disabled_actions()
    return gr.update(interactive=True), gr.update(interactive=True)


def select_candidate(
    evt: gr.SelectData,
    candidates: Sequence[Sequence[Any]] | None,
    candidate_items: Sequence[Mapping[str, Any]] | None,
    state: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None, Any, Any, str]:
    """选中候选题：渲染完整预览并按权威状态启用或禁用审核操作。"""

    items = list(candidate_items or [])
    index = int(getattr(evt, "index", 0) or 0)
    try:
        _ensure_teacher(state)
    except PermissionDeniedError as error:
        return empty_state("尚未选择候选题。"), None, *_disabled_actions(), _error_message(error)
    if not items or index >= len(items):
        message = feedback("未选中任何候选题。", "warning")
        return empty_state("尚未选择候选题。"), None, *_disabled_actions(), message
    item = items[index]
    status = str(_value(item, "status", ""))
    if status in {QuestionStatus.PENDING_REVIEW.value, QuestionStatus.PENDING_REVIEW.name}:
        message = feedback("该候选题待教师审核：可通过或退回修订。", "info")
    else:
        message = feedback(NOT_PENDING_REVIEW_MESSAGE, "warning")
    return (
        candidate_preview_markdown(item),
        dict(item),
        *_action_updates(items, index),
        message,
    )


def submit_candidate_review_action(
    action: str,
    selected: Mapping[str, Any] | None,
    comment: str | None,
    course_id: str | None,
    candidate_status: Any,
    state: Mapping[str, Any] | None,
) -> tuple[str, list[list[str]], list[dict[str, Any]], str, Any, Any]:
    """提交审核结论并按权威结果刷新列表与预览。"""

    candidate_id = str((selected or {}).get("candidate_id") or "").strip()
    try:
        _ensure_teacher(state)
    except PermissionDeniedError as error:
        return _error_message(error), *_unchanged_list(course_id, candidate_status, state)
    if not candidate_id:
        message = feedback("请先在候选题列表中选中一条记录。", "warning")
        return message, *_unchanged_list(course_id, candidate_status, state)
    if action == "request_revision" and not str(comment or "").strip():
        message = feedback("退回修订必须填写修订意见。", "warning")
        return message, *_unchanged_list(course_id, candidate_status, state)
    try:
        outcome = _active_loaders.review(
            state,
            candidate_id=candidate_id,
            action=action,
            comment=comment,
            expected_status=QuestionStatus.PENDING_REVIEW,
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        QuestionGenerationError,
        SQLAlchemyError,
    ) as error:
        message = _error_message(error)
        return message, *_unchanged_list(course_id, candidate_status, state)
    assert isinstance(outcome, CandidateReviewOutcomeDTO)
    if outcome.decision == "request_revision":
        message = feedback(
            f"已退回修订，当前状态：{_status_text(outcome.status)}。{COMMENT_NOT_PERSISTED_NOTE}",
            "warning",
        )
    else:
        message = feedback(
            f"审核通过，当前状态：{_status_text(outcome.status)}。", "success"
        )
    rows, items, _list_message = refresh_candidate_list(course_id, candidate_status, state)
    return message, rows, items, empty_state("尚未选择候选题。"), *_disabled_actions()


def _unchanged_list(
    course_id: str | None,
    candidate_status: Any,
    state: Mapping[str, Any] | None,
) -> tuple[list[list[str]], list[dict[str, Any]], str, Any, Any]:
    """审核未生效时保持列表事实不变，仅返回当前真实列表。"""

    rows, items, _ = refresh_candidate_list(course_id, candidate_status, state)
    return rows, items, empty_state("尚未选择候选题。"), *_disabled_actions()


def create_question_generation_view(
    session_state: Any | None = None,
) -> QuestionGenerationView:
    """创建左条件、右候选与来源折叠区的 AI 出题面板。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False, elem_classes="edu-question-generation") as panel:
        gr.Markdown("## AI 出题")
        current_course = gr.Markdown(empty_state("尚未选择课程。"))
        with gr.Row():
            with gr.Column(scale=30, min_width=260):
                course = gr.Dropdown(label="课程", choices=[])
                knowledge_point = gr.Textbox(
                    label="知识点", placeholder="多个知识点用逗号分隔"
                )
                difficulty = gr.Dropdown(
                    label="难度",
                    choices=["简单", "中等", "困难"],
                    value="中等",
                )
                question_type = gr.Dropdown(
                    label="题型",
                    choices=status_choices(
                        [item.value for item in QuestionType],
                        entity="question_type",
                    ),
                    value=QuestionType.SHORT_ANSWER.value,
                )
                amount = gr.Number(label="数量", value=1, minimum=1, precision=0)
                refresh_button = gr.Button("刷新课程", variant="secondary")
                generate_button = gr.Button("生成候选题", variant="primary")
                status_filter = gr.Dropdown(
                    label="候选状态",
                    choices=status_choices(
                        [item.value for item in CANDIDATE_STATUS_SCOPE],
                        entity="question",
                        include_all=True,
                    ),
                    value="",
                )
                list_button = gr.Button("刷新候选列表", variant="secondary")
                generation_state = gr.Markdown(empty_state("尚未生成候选题。"))
            with gr.Column(scale=70, min_width=420):
                candidates = gr.Dataframe(
                    headers=list(CANDIDATE_HEADERS),
                    datatype=["str"] * len(CANDIDATE_HEADERS),
                    value=[],
                    interactive=False,
                    label="候选题列表（待教师审核）",
                    elem_classes=["candidate-list"],
                )
                candidate_items = gr.State([])
                selected_candidate = gr.State(None)
                candidate_preview = gr.Markdown(empty_state("尚未选择候选题。"))
                with gr.Row():
                    approve_button = gr.Button(
                        "审核通过", variant="primary", interactive=False
                    )
                    revision_button = gr.Button("退回修订", interactive=False)
                comment = gr.Textbox(
                    label="修订意见",
                    lines=3,
                    placeholder="退回修订时必填",
                )
        with gr.Accordion("检索来源", open=False):
            evidence = gr.Markdown(empty_state("尚未生成候选题，暂无检索依据。"))
        message = gr.Markdown(empty_state("请选择课程后开始出题。"))

        refresh_button.click(
            refresh_generation_context,
            inputs=[state],
            outputs=[course, message],
            show_progress="hidden",
        )
        course.change(
            show_course_context,
            inputs=[course, state],
            outputs=[current_course],
            show_progress="hidden",
        )
        course.change(
            refresh_candidate_list,
            inputs=[course, status_filter, state],
            outputs=[candidates, candidate_items, message],
            show_progress="hidden",
        )
        list_button.click(
            refresh_candidate_list,
            inputs=[course, status_filter, state],
            outputs=[candidates, candidate_items, message],
            show_progress="hidden",
        )
        generate_button.click(
            generate_candidates,
            inputs=[course, knowledge_point, difficulty, question_type, amount, state],
            outputs=[
                candidates,
                candidate_items,
                generation_state,
                candidate_preview,
                approve_button,
                revision_button,
                evidence,
                message,
            ],
            show_progress="hidden",
        )
        candidates.select(
            select_candidate,
            inputs=[candidates, candidate_items, state],
            outputs=[
                candidate_preview,
                selected_candidate,
                approve_button,
                revision_button,
                message,
            ],
            show_progress="hidden",
        )
        approve_button.click(
            lambda selected, comment_value, course_value, status_value, current_state: (
                submit_candidate_review_action(
                    "approve",
                    selected,
                    comment_value,
                    course_value,
                    status_value,
                    current_state,
                )
            ),
            inputs=[selected_candidate, comment, course, status_filter, state],
            outputs=[
                message,
                candidates,
                candidate_items,
                candidate_preview,
                approve_button,
                revision_button,
            ],
            show_progress="hidden",
        )
        revision_button.click(
            lambda selected, comment_value, course_value, status_value, current_state: (
                submit_candidate_review_action(
                    "request_revision",
                    selected,
                    comment_value,
                    course_value,
                    status_value,
                    current_state,
                )
            ),
            inputs=[selected_candidate, comment, course, status_filter, state],
            outputs=[
                message,
                candidates,
                candidate_items,
                candidate_preview,
                approve_button,
                revision_button,
            ],
            show_progress="hidden",
        )

    return QuestionGenerationView(
        panel=panel,
        candidates_table=candidates,
        message=message,
        candidate_preview=candidate_preview,
        evidence=evidence,
        generation_state=generation_state,
        selected_candidate=selected_candidate,
        candidate_rows=candidate_items,
        approve_button=approve_button,
        revision_button=revision_button,
    )


build_question_generation_view = create_question_generation_view
configure_production_question_generation_loaders = configure_question_generation_loaders

__all__ = [
    "CANDIDATE_HEADERS",
    "COMMENT_NOT_PERSISTED_NOTE",
    "CONDITION_HEADERS",
    "GENERATION_UNAVAILABLE_MESSAGE",
    "INSUFFICIENT_CONTEXT_MESSAGE",
    "NOT_PENDING_REVIEW_MESSAGE",
    "NO_CANDIDATE_MESSAGE",
    "QuestionGenerationLoaders",
    "QuestionGenerationView",
    "build_question_generation_view",
    "candidate_preview_markdown",
    "candidate_rows",
    "configure_production_question_generation_loaders",
    "configure_question_generation_loaders",
    "create_question_generation_view",
    "generate_candidates",
    "refresh_candidate_list",
    "refresh_generation_context",
    "select_candidate",
    "show_course_context",
    "submit_candidate_review_action",
]
