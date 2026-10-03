"""T167 shared presentation of persisted reports; service facts remain authoritative."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

import gradio as gr
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from backend.app.domain.enums import QuestionStatus
from backend.app.domain.permissions import PermissionDeniedError
from backend.app.schemas.content_validation import ValidationReportView
from backend.app.services.auth_service import AuthenticationError
from backend.app.services.course_service import CourseServiceError
from backend.app.services.file_storage_service import FileStorageError
from backend.app.ui import question_review_loaders as loaders
from backend.app.ui.layout_view import feedback, status_badge, status_label
from backend.app.ui.paper_image_review_view import (
    assessment_summary,
    manual_check_payload,
)

_CHECK_LABELS = {
    "answer_correctness": "答案正确性",
    "condition_sufficiency": "条件充分性",
    "option_ambiguity": "选项歧义",
    "rubric_clarity": "评分标准清晰性",
}
_VERDICTS = {
    "pass": "通过",
    "fail": "未通过",
    "insufficient_evidence": "依据不足",
    "needs_review": "待核对",
}
_OUTCOMES = {
    "running": "核验执行中",
    "passed": "核验通过",
    "failed": "核验未通过",
    "technical_error": "核验技术失败",
}


def _value(value: Any, key: str, default: Any = None) -> Any:
    return (
        value.get(key, default)
        if isinstance(value, Mapping)
        else getattr(value, key, default)
    )


def _text(value: Any) -> str:
    return escape(str(value))


def approval_enabled(detail: Any) -> bool:
    """Only the server's explicit current gate enables approval, never a local report guess."""
    return _value(detail, "can_review") is True and _value(detail, "status") in {
        QuestionStatus.PENDING_REVIEW,
        QuestionStatus.PENDING_REVIEW.value,
    }


def semantic_report_html(report: ValidationReportView) -> str:
    state = "当前报告" if report.is_current else "历史报告"
    if report.stale:
        state += "（过期）"
    gate = "当前可批准" if report.can_review else "当前不可批准"
    body = [
        '<section class="edu-surface edu-semantic-report">',
        f"<h3>{_text(_OUTCOMES[report.outcome])} · {_text(state)}</h3>",
        f"<p>核验轮次 {report.run_no} · 输入修订 {report.input_revision} · {gate}</p>",
        f"<p>真实执行来源：{_text(report.executor_name)}</p>",
    ]
    provenance = report.provenance
    body.append(
        "<p>实际 Provider / 模型 / Prompt："
        + " / ".join(
            _text(item or "未知")
            for item in (
                provenance.provider_name,
                provenance.model,
                provenance.prompt_version,
            )
        )
        + "</p>"
    )
    body.append(
        f"<p>开始：{_text(report.created_at.isoformat())}；完成：{_text(report.completed_at.isoformat() if report.completed_at else '尚未结束')}</p>"
    )
    if report.error:
        body.append(
            f'<p role="alert">{_text(report.error.code)}：{_text(report.error.message)}（{_text(report.error.stage)}）</p>'
        )
    if report.checks is None:
        body.append("<p>未产生合法分项结果。</p>")
    else:
        body.append("<dl>")
        for check in report.checks:
            body.append(
                f"<dt>{_text(_CHECK_LABELS[check.kind])}：{_text(_VERDICTS[check.verdict])}</dt><dd>{_text(check.reason)}</dd>"
            )
        body.append("</dl>")
    if report.issues is None:
        body.append("<p>尚无合法问题清单。</p>")
    elif not report.issues:
        body.append("<p>本轮未报告问题。</p>")
    else:
        body.append("<h4>本轮问题</h4><ul>")
        for issue in report.issues:
            body.append(
                f"<li>{_text(issue.code)}：{_text(issue.message)}（{_text(issue.severity)}；{_text(issue.field or '未指定字段')}）</li>"
            )
        body.append("</ul>")
    if report.input_refs.evidence:
        body.append("<h4>本轮实际依据</h4>")
        for item in report.input_refs.evidence:
            data = item.source_data
            if item.kind in {"chunk", "question_source_chunk"}:
                body.append(
                    f'<p>{_text(data["source_file"])} · {_text(json.dumps(data["location"], ensure_ascii=False))}</p><div class="edu-question-body">{_text(data["content_snapshot"])}</div>'
                )
            else:
                body.append(
                    f'<p>本轮题图依据：{_text(item.source_id)}</p><div class="edu-question-body">{_text(json.dumps(data["confirmed_conditions"], ensure_ascii=False))}</div>'
                )
    if report.manual_dispositions:
        body.append("<h4>真实教师处置</h4>")
        for disposition in report.manual_dispositions:
            body.append(
                f"<p>{_text(disposition.handled_by)} · {_text(disposition.handled_at.isoformat())} · {_text(disposition.action)}</p><p>{_text(disposition.reason)}</p>"
            )
    body.append("</section>")
    return "".join(body)


def semantic_history_html(reports: Sequence[ValidationReportView]) -> str:
    if not reports:
        return '<section class="edu-surface"><p>历史核验未知：尚无已保存的语义核验报告。</p></section>'
    return "".join(semantic_report_html(report) for report in reports)


_SOURCE_LABELS = {
    "manual": "人工创建",
    "ai_generated": "AI 生成",
    "paper_imported": "试卷导入",
    "adapted": "原题改编",
}


def question_details_html(detail: Any) -> str:
    """Display stored source facts; historical unknowns stay unknown."""
    source_type = _value(detail, "source_type")
    body = ['<section class="edu-surface"><h3>来源与解析</h3>']
    status = _value(detail, "status")
    if status is not None:
        body.append(
            f"<p>当前题目状态：{_text(status_label(status, entity='question'))}</p>"
        )
    body.append(
        f"<p>创建路径：{_text(_SOURCE_LABELS.get(str(source_type), str(source_type or '历史未知')))}</p>"
    )
    body.append(
        f'<h4>解析</h4><div class="edu-question-body">{_text(_value(detail, "analysis") or "尚未提供解析")}</div>'
    )
    for source in _value(detail, "sources", []) or []:
        body.append(
            f"<h4>{_text(_value(source, 'source_file'))} · 片段 {_text(_value(source, 'chunk_index'))}</h4>"
        )
        location = _value(source, "location_snapshot")
        body.append(
            f"<p>原始位置：{_text(json.dumps(location, ensure_ascii=False)) if location is not None else '历史位置未知'}</p>"
        )
        body.append(
            f'<div class="edu-question-body">{_text(_value(source, "content_snapshot", ""))}</div>'
        )
        if _value(source, "source_deleted"):
            body.append("<p>来源已删除，保留历史快照。</p>")
    if not _value(detail, "sources", []):
        source_status = _value(detail, "source_status")
        body.append(
            "<p>"
            + (
                "历史教学来源未知。"
                if source_status == "history_unknown"
                else "尚无已保存的教学片段。"
            )
            + "</p>"
        )
    for parent in _value(detail, "parent_sources", []) or []:
        body.append(
            f"<p>父题：{_text(_value(parent, 'source_question_id'))} · 改编方式：{_text(_value(parent, 'adaptation_type'))}</p>"
        )
    paper = _value(detail, "paper_source")
    if paper is not None:
        body.append(
            f"<p>原卷：{_text(_value(paper, 'paper_import_id'))} · 原题号：{_text(_value(paper, 'question_number') or '未知')}</p>"
        )
        body.append(
            f"<p>实际来源页：{_text(', '.join(str(item) for item in _value(paper, 'source_page_ids', [])))}</p>"
        )
    body.append("</section>")
    report = _value(detail, "current_validation")
    body.append(
        semantic_report_html(report)
        if report is not None
        else semantic_history_html([])
    )
    image = _value(detail, "image_assessment")
    if image is not None:
        body.append(
            f'<section class="edu-surface"><h3>图像核对</h3><div class="edu-question-body">{_text(assessment_summary(image))}</div>{image_evidence_html(image)}</section>'
        )
    return "".join(body)


def image_evidence_html(view) -> str:
    body = []
    if view.current_run is not None and view.current_run.result is not None:
        body.append("<h4>实际机器理解（待教师核对）</h4>")
        for observation in view.current_run.result.observations:
            body.append(
                f"<p>图 {observation.image_index}：{_text(observation.description)}</p>"
            )
        for condition in view.current_run.result.conditions:
            body.append(
                f"<p>图 {condition.image_index}：{_text(condition.text)} · 条件 {_text(condition.condition_id)}</p>"
            )
    if view.confirmed_conditions:
        body.append("<h4>当前真实教师确认条件</h4>")
        for condition in view.confirmed_conditions:
            body.append(
                f"<p>{_text(condition.text)} · {_text(condition.evidence_region.bbox if condition.evidence_region else '边界未知')}</p>"
            )
    if view.current_check:
        body.append(f"<p>本次教师说明：{_text(view.current_check.explanation)}</p>")
        for finding in view.current_check.image_findings:
            body.append(f"<p>{_text(finding.finding)}：{_text(finding.reason)}</p>")
    for issue in view.open_issues:
        body.append(
            f"<p>未解决问题 {_text(issue.issue_id)}：{_text(issue.message)}</p>"
        )
    return "".join(body)


@dataclass
class QuestionReviewPanel:
    outputs: list[Any]
    render: Any
    clear: Any
    teaching_chunk_ids: Any


def teaching_chunks_html(chunks: Sequence[Mapping[str, Any]]) -> str:
    if not chunks:
        return "<p>未选择本次教学依据；显式空选择无法执行核验。</p>"
    body = []
    for chunk in chunks:
        position = chunk.get("location")
        body.append(
            f"<h4>{_text(chunk['source_file'])} · 片段 {_text(chunk['chunk_index'])}</h4>"
            f"<p>位置：{_text(json.dumps(position, ensure_ascii=False)) if position is not None else '未知'}</p>"
            f'<div class="edu-question-body">{_text(chunk["content"])}</div>'
        )
    return "".join(body)


def create_question_review_panel(
    selected_id,
    state,
    *,
    prefix="edu-question",
    approval_button=None,
    editing_state=None,
    validation_button=None,
    revision_button=None,
    submit_button=None,
    question_snapshot=None,
    status_display=None,
) -> QuestionReviewPanel:
    """Existing question editor owns selection; commands read its current persisted ID."""
    editing_state = editing_state if editing_state is not None else gr.State(False)
    facts = gr.HTML("", elem_id=f"{prefix}-review-facts")
    history = gr.HTML("")
    report_snapshot = gr.State(None)
    teaching_selection = gr.State(None)
    teaching_records = gr.State([])
    with gr.Accordion("本次语义核验的教学依据", open=False) as teaching_basis:
        gr.Markdown(
            "默认复用已登记教学来源。人工题和导入题可显式选择当前课程 Ready 资料的真实片段。选择只在执行核验时保存；不能用题目标签代替教学依据。"
        )
        with gr.Row():
            load_basis = gr.Button("加载本课程教学片段", interactive=False)
            reuse_basis = gr.Button("复用已登记教学依据")
        teaching_picker = gr.Dropdown(
            label="文件、实际位置与内容摘要", choices=[], multiselect=True, value=[]
        )
        teaching_preview = gr.HTML("")
    with gr.Accordion("父题与原卷来源查看", open=False):
        load_source = gr.Button("查看父题与来源原页", interactive=False)
        source_facts = gr.HTML("")
        source_pages = gr.Dropdown(label="实际来源原页", choices=[])
        show_page = gr.Button("查看所选原页", interactive=False)
        source_image = gr.HTML("")
    with gr.Accordion("本轮问题的教师处置", open=False, visible=False) as dispositions:
        problem = gr.Dropdown(label="当前分项或问题", choices=[])
        action = gr.Dropdown(
            label="处置动作",
            choices=[
                ("补充本轮依据后重核验", "provide_evidence"),
                ("说明问题处理依据后重核验", "resolve_issue"),
                ("退回修订", "request_revision"),
            ],
            value="provide_evidence",
        )
        evidence = gr.CheckboxGroup(label="本轮实际依据", choices=[])
        reason = gr.Textbox(label="真实教师处置理由", lines=3)
        save_disposition = gr.Button("保存处置（不直接批准）", interactive=False)
    with gr.Row():
        reload_history = gr.Button("查看核验历史", interactive=False)
    with gr.Accordion("正式题原图与教师核对", open=False, visible=False) as images:
        image_snapshot = gr.State(None)
        image_summary = gr.Markdown("")
        originals = gr.HTML("")
        with gr.Row():
            reload_images = gr.Button("读取当前原图与核对")
            image_task = gr.Textbox(label="图片理解任务", value="提取题图实际可见条件")
            understand = gr.Button("理解当前题图", interactive=False)
        image_status = gr.Radio(
            label="本次核对状态",
            choices=[("已确认", "confirmed"), ("仍有未解决问题", "unresolved")],
            value="unresolved",
        )
        findings = gr.Dataframe(
            label="逐图核对结果",
            headers=["图序", "结论", "真实理由"],
            datatype=["str"] * 3,
            type="array",
            value=[],
            column_count=3,
            row_count=1,
        )
        gr.Markdown("结论可填：条件已确认、无需额外条件、仍未确定。逐图填写实际理由。")
        conditions = gr.Dataframe(
            label="教师确认的题图条件",
            headers=[
                "图序",
                "原图中的条件",
                "像素坐标（未知留空）",
                "原理解条件 ID（可空）",
            ],
            datatype=["str"] * 4,
            type="array",
            value=[],
            column_count=4,
            row_count=1,
        )
        issues = gr.Dataframe(
            label="教师发现的问题",
            headers=["图序（逗号分隔，可空）", "问题说明"],
            datatype=["str"] * 2,
            type="array",
            value=[],
            column_count=2,
            row_count=1,
        )
        resolutions = gr.Dataframe(
            label="已记录问题处置",
            headers=["问题 ID", "已解决 / 未解决", "真实依据"],
            datatype=["str"] * 3,
            type="array",
            value=[],
            column_count=3,
            row_count=1,
        )
        explanation = gr.Textbox(label="本次真实教师核对说明", lines=3)
        check = gr.Button("保存教师题图核对", interactive=False)
    message = gr.Markdown("")
    outputs = [
        facts,
        history,
        report_snapshot,
        teaching_selection,
        teaching_records,
        teaching_basis,
        load_basis,
        teaching_picker,
        teaching_preview,
        load_source,
        source_facts,
        source_pages,
        show_page,
        source_image,
        dispositions,
        problem,
        action,
        evidence,
        reason,
        save_disposition,
        reload_history,
        images,
        image_snapshot,
        image_summary,
        originals,
        understand,
        check,
        findings,
        conditions,
        issues,
        resolutions,
        explanation,
        image_status,
        message,
    ]
    for button in (approval_button, validation_button, revision_button, submit_button):
        if button is not None:
            outputs.append(button)
    for component in (question_snapshot, status_display):
        if component is not None:
            outputs.append(component)
    errors = (
        AuthenticationError,
        PermissionDeniedError,
        FileStorageError,
        HTTPException,
        SQLAlchemyError,
        CourseServiceError,
        ValueError,
    )

    def clear():
        result = {
            facts: "",
            history: "",
            report_snapshot: None,
            teaching_selection: None,
            teaching_records: [],
            teaching_basis: gr.update(visible=True),
            load_basis: gr.update(interactive=False),
            teaching_picker: gr.update(choices=[], value=[]),
            teaching_preview: "",
            load_source: gr.update(interactive=False),
            source_facts: "",
            source_pages: gr.update(choices=[], value=None),
            show_page: gr.update(interactive=False),
            source_image: "",
            dispositions: gr.update(visible=False),
            problem: gr.update(choices=[], value=None),
            action: "provide_evidence",
            evidence: gr.update(choices=[], value=[]),
            reason: "",
            save_disposition: gr.update(interactive=False),
            reload_history: gr.update(interactive=False),
            images: gr.update(visible=False),
            image_snapshot: None,
            image_summary: "",
            originals: "",
            understand: gr.update(interactive=False),
            check: gr.update(interactive=False),
            findings: [],
            conditions: [],
            issues: [],
            resolutions: [],
            explanation: "",
            image_status: "unresolved",
            message: "",
        }

        for button in (
            approval_button,
            validation_button,
            revision_button,
            submit_button,
        ):
            if button is not None:
                result[button] = gr.update(interactive=False)
        return result

    def render(detail, *, editing=False):
        result = clear()
        result[facts] = question_details_html(detail)
        result[load_basis] = gr.update(
            interactive=not editing and detail.status == QuestionStatus.PENDING_REVIEW
        )
        result[load_source] = gr.update(interactive=True)
        if question_snapshot is not None:
            result[question_snapshot] = detail.model_dump(mode="json")
        if status_display is not None:
            result[status_display] = status_badge(detail.status, entity="question")
        if approval_button is not None:
            result[approval_button] = gr.update(
                interactive=not editing and approval_enabled(detail)
            )
        if validation_button is not None:
            result[validation_button] = gr.update(
                interactive=not editing
                and detail.status == QuestionStatus.PENDING_REVIEW
            )
        if revision_button is not None:
            result[revision_button] = gr.update(
                interactive=not editing
                and detail.status
                in {QuestionStatus.PENDING_REVIEW, QuestionStatus.APPROVED}
            )
        if submit_button is not None:
            result[submit_button] = gr.update(
                interactive=not editing
                and detail.status
                in {QuestionStatus.DRAFT, QuestionStatus.NEEDS_REVISION}
            )
        report = _value(detail, "current_validation")
        if report is not None:
            result[report_snapshot] = report.model_dump(mode="json")
            result[dispositions] = gr.update(visible=True)
            choices = [
                (
                    _CHECK_LABELS[item.kind] + "：" + _VERDICTS[item.verdict],
                    "check:" + item.kind,
                )
                for item in report.checks or []
            ]
            choices.extend(
                (item.code + "：" + item.message, "issue:" + str(item.issue_id))
                for item in report.issues or []
            )
            result[problem] = gr.update(choices=choices, value=None)
            result[evidence] = gr.update(
                choices=[
                    (
                        str(item.source_data.get("source_file") or "本轮题图依据"),
                        str(item.evidence_id),
                    )
                    for item in report.input_refs.evidence
                ],
                value=[],
            )
            result[save_disposition] = gr.update(
                interactive=not editing
                and report.is_current
                and not report.stale
                and report.outcome in {"passed", "failed"}
            )
        result[reload_history] = gr.update(interactive=True)
        image = _value(detail, "image_assessment")
        if image is not None and image.input_refs is not None:
            result[images] = gr.update(visible=True)
            result[image_snapshot] = image.model_dump(mode="json")
            result[image_summary] = assessment_summary(image)
            editable = not editing and detail.status in {
                QuestionStatus.DRAFT,
                QuestionStatus.PENDING_REVIEW,
                QuestionStatus.NEEDS_REVISION,
            }
            result[understand] = gr.update(interactive=editable)
            result[check] = gr.update(interactive=editable and image.evidence_readable)
            result[findings] = [
                [str(index), "", ""]
                for index in range(1, len(image.input_refs.images) + 1)
            ]
        return result

    def identity_of(value):
        return (
            str(_value(value, "candidate_id", _value(value, "id", "")))
            if isinstance(value, Mapping)
            else str(value or "")
        )

    def load_teaching_basis(identity, current_state):
        try:
            chunks = loaders.teaching_chunks(identity_of(identity), current_state)
            choices = []
            for chunk in chunks:
                location = chunk.get("location")
                position = (
                    json.dumps(location, ensure_ascii=False)
                    if location is not None
                    else "位置未知"
                )
                label = f"{chunk['source_file']} · 片段 {chunk['chunk_index']} · {position} · {chunk['content'][:120]}"
                choices.append((label, str(chunk["id"])))
            return {
                teaching_records: chunks,
                teaching_picker: gr.update(choices=choices, value=[]),
                teaching_selection: None,
                teaching_preview: "请选择真实片段；尚未选择时继续复用已登记依据。",
                message: "",
            }
        except errors as error:
            return {
                message: feedback(
                    "数据库访问失败，请稍后重试。"
                    if isinstance(error, SQLAlchemyError)
                    else str(getattr(error, "detail", error)),
                    "error",
                )
            }

    def select_teaching_basis(chunk_ids, records):
        chosen = list(chunk_ids or [])
        by_id = {str(chunk["id"]): chunk for chunk in records or []}
        if any(identity not in by_id for identity in chosen):
            return {
                teaching_selection: [],
                teaching_preview: "",
                message: feedback("请选择已加载的真实教学片段。", "error"),
            }
        return {
            teaching_selection: chosen,
            teaching_preview: teaching_chunks_html(
                [by_id[identity] for identity in chosen]
            ),
            message: "",
        }

    def reset_teaching_basis():
        return {
            teaching_selection: None,
            teaching_picker: gr.update(value=[]),
            teaching_preview: "本次核验将复用已登记教学依据。",
        }

    def show_source_context(identity, current_state):
        try:
            context = loaders.source_context(identity_of(identity), current_state)
            parents = context["parents"]
            body = []
            for parent in parents:
                body.append(
                    f"<h4>父题当前内容 · {_text(status_label(parent['current_status'], entity='question'))}</h4>"
                    f'<div class="edu-question-body">{_text(parent["current_content"])}</div>'
                    f"<p>改编方式：{_text(parent['adaptation_type'])}</p>"
                )
            if not parents:
                body.append("<p>该题没有已登记父题。</p>")
            pages = context["pages"]
            if not pages:
                body.append("<p>该题没有直接原卷来源页；改编来源保留在实际父题。</p>")
            return {
                source_facts: "".join(body),
                source_pages: gr.update(
                    choices=[
                        (
                            f"原卷第 {page['page_number']} 页 · {page['width']}×{page['height']} 像素",
                            page["id"],
                        )
                        for page in pages
                    ],
                    value=None,
                ),
                show_page: gr.update(interactive=bool(pages)),
                source_image: "",
                message: "",
            }
        except errors as error:
            return {
                source_image: "",
                message: feedback(
                    "数据库访问失败，请稍后重试。"
                    if isinstance(error, SQLAlchemyError)
                    else str(getattr(error, "detail", error)),
                    "error",
                ),
            }

    def show_source_page(identity, page_id, current_state):
        try:
            if not page_id:
                raise ValueError("请选择该题实际来源原页。")
            return {
                source_image: loaders.source_page_html(
                    identity_of(identity), str(page_id), current_state
                ),
                message: "",
            }
        except errors as error:
            return {
                source_image: "",
                message: feedback(
                    "数据库访问失败，请稍后重试。"
                    if isinstance(error, SQLAlchemyError)
                    else str(getattr(error, "detail", error)),
                    "error",
                ),
            }

    def show_history(identity, current_state):
        identity = identity_of(identity)
        try:
            return {
                history: semantic_history_html(
                    loaders.history(identity, current_state)
                ),
                message: "",
            }
        except errors as error:
            return {
                history: "",
                message: feedback(
                    "数据库访问失败，请稍后重试。"
                    if isinstance(error, SQLAlchemyError)
                    else str(getattr(error, "detail", error)),
                    "error",
                ),
            }

    def show_images(identity, current_state, editing=False):
        identity = identity_of(identity)
        try:
            detail = loaders.detail(identity, current_state)
            result = render(detail, editing=bool(editing))
            image = detail.image_assessment
            if image is not None and image.input_refs is not None:
                result[originals] = "".join(
                    f"<p>图 {item.image_index}</p>"
                    + loaders.image_html(identity, str(item.asset_id), current_state)
                    for item in image.input_refs.images
                )
            return result
        except errors as error:
            result = clear()
            result[message] = feedback(
                "数据库访问失败，请稍后重试。"
                if isinstance(error, SQLAlchemyError)
                else str(getattr(error, "detail", error)),
                "error",
            )
            return result

    def save_check(
        identity,
        snapshot,
        status,
        finding_rows,
        condition_rows,
        issue_rows,
        resolution_rows,
        reason,
        current_state,
        editing,
    ):
        try:
            if editing:
                raise ValueError("请先保存题目文本，再核对题图。")
            identity = identity_of(identity)
            command = manual_check_payload(
                snapshot,
                status,
                finding_rows,
                condition_rows,
                issue_rows,
                resolution_rows,
                reason,
            )
            loaders.check_images(identity, command, current_state)
            result = show_images(identity, current_state)
            if not result.get(message):
                result[message] = feedback(
                    "已保存真实题图核对；语义报告已失效，请重新核验后批准。", "success"
                )
            return result
        except errors as error:
            return {
                message: feedback(
                    "数据库访问失败，请稍后重试。"
                    if isinstance(error, SQLAlchemyError)
                    else str(getattr(error, "detail", error)),
                    "error",
                )
            }

    async def understand_current(identity, task, current_state, editing):
        identity = identity_of(identity)
        try:
            if editing:
                raise ValueError("请先保存题目文本，再理解题图。")
            await loaders.understand_images(identity, task, current_state)
            return show_images(identity, current_state)
        except errors as error:
            return {
                message: feedback(
                    "数据库访问失败，请稍后重试。"
                    if isinstance(error, SQLAlchemyError)
                    else str(getattr(error, "detail", error)),
                    "error",
                )
            }

    def save_teacher_disposition(
        identity,
        snapshot,
        target,
        action_value,
        proof,
        reason_value,
        current_state,
        editing,
    ):
        try:
            if editing:
                raise ValueError("请先保存题目文本，再处置核验问题。")
            if snapshot is None or not target:
                raise ValueError("请选择当前已结束报告中的分项或问题。")
            report = ValidationReportView.model_validate(snapshot)
            request = {
                "action": action_value,
                "reason": reason_value,
                "evidence_refs": proof or [],
            }
            kind, target_id = target.split(":", 1)
            request["check_kind" if kind == "check" else "issue_ids"] = (
                target_id if kind == "check" else [target_id]
            )
            identity = identity_of(identity)
            loaders.dispose_validation(identity, str(report.id), request, current_state)
            result = render(loaders.detail(identity, current_state))
            result[message] = feedback(
                "已保存真实教师处置；请按当前状态修订或重新核验，处置不会直接批准。",
                "info",
            )
            return result
        except errors as error:
            return {
                message: feedback(
                    "数据库访问失败，请稍后重试。"
                    if isinstance(error, SQLAlchemyError)
                    else str(getattr(error, "detail", error)),
                    "error",
                )
            }

    load_basis.click(
        load_teaching_basis,
        inputs=[selected_id, state],
        outputs=outputs,
        show_progress="hidden",
    )
    teaching_picker.input(
        select_teaching_basis,
        inputs=[teaching_picker, teaching_records],
        outputs=outputs,
        show_progress="hidden",
    )
    reuse_basis.click(
        reset_teaching_basis, inputs=[], outputs=outputs, show_progress="hidden"
    )
    load_source.click(
        show_source_context,
        inputs=[selected_id, state],
        outputs=outputs,
        show_progress="hidden",
    )
    show_page.click(
        show_source_page,
        inputs=[selected_id, source_pages, state],
        outputs=outputs,
        show_progress="hidden",
    )

    save_disposition.click(
        save_teacher_disposition,
        inputs=[
            selected_id,
            report_snapshot,
            problem,
            action,
            evidence,
            reason,
            state,
            editing_state,
        ],
        outputs=outputs,
        show_progress="minimal",
    )

    reload_history.click(
        show_history,
        inputs=[selected_id, state],
        outputs=outputs,
        show_progress="hidden",
    )
    reload_images.click(
        show_images,
        inputs=[selected_id, state, editing_state],
        outputs=outputs,
        show_progress="hidden",
    )
    check.click(
        save_check,
        inputs=[
            selected_id,
            image_snapshot,
            image_status,
            findings,
            conditions,
            issues,
            resolutions,
            explanation,
            state,
            editing_state,
        ],
        outputs=outputs,
        show_progress="minimal",
    )
    understand.click(
        understand_current,
        inputs=[selected_id, image_task, state, editing_state],
        outputs=outputs,
        show_progress="minimal",
    )
    return QuestionReviewPanel(outputs, render, clear, teaching_selection)
