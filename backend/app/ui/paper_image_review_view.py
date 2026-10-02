"""Whole-group image facts and explicit teacher check in the correction editor."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

import gradio as gr
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from backend.app.domain.permissions import PermissionDeniedError
from backend.app.schemas.image_assessment import (
    ImageAssessmentView,
    ImageManualCheckRequest,
)
from backend.app.services.auth_service import AuthenticationError
from backend.app.services.file_storage_service import FileStorageError
from backend.app.ui import paper_import_loaders as loaders

FINDINGS = {"conditions_confirmed", "no_conditions_needed", "unresolved"}
FINDING_LABELS = {
    "条件已确认": "conditions_confirmed",
    "无需额外条件": "no_conditions_needed",
    "仍未确定": "unresolved",
}
RESOLUTION_LABELS = {"已解决": "resolved", "未解决": "unresolved"}
STATUS_TEXT = {
    "pending": "待教师核对",
    "confirmed": "当前核对已确认",
    "unresolved": "当前核对有未解决问题",
    "not_required": "本题未关联题图",
}


def _filled(rows):
    return [row for row in rows or [] if any(value not in (None, "") for value in row)]


def _asset(index, images):
    text = str(index or "").strip()
    if not text.isdigit() or not 1 <= int(text) <= len(images):
        raise ValueError("图序必须来自本次读取的整组题图。")
    return images[int(text) - 1].asset_id


def _region(text):
    if not str(text or "").strip():
        return None
    values = [float(x.strip()) for x in str(text).split(",")]
    if len(values) != 4:
        raise ValueError(
            "证据坐标须为来源页像素 x0,y0,x1,y1（无来源页时使用资产原图）；未知留空。"
        )
    return {"bbox": values}


def manual_check_payload(
    snapshot, status, findings, conditions, issues, resolutions, explanation
):
    """Use the displayed version and group, never silently reload stale teacher work."""
    view = ImageAssessmentView.model_validate(snapshot)
    if view.input_refs is None:
        raise ValueError("请选择有真实题图的待校正题目。")
    images = view.input_refs.images
    payload = {
        "expected_context_revision": view.context_revision,
        "expected_run_no": view.run_no,
        "expected_check_no": view.check_no,
        "status": status,
        "image_findings": [
            {
                "asset_id": _asset(index, images),
                "finding": FINDING_LABELS.get(
                    str(finding or "").strip(), str(finding or "").strip()
                ),
                "reason": str(reason or "").strip(),
            }
            for index, finding, reason in _filled(findings)
        ],
        "confirmed_conditions": [
            {
                "asset_id": _asset(index, images),
                "text": str(text or "").strip(),
                "evidence_region": _region(region),
                "source_condition_id": str(source or "").strip() or None,
            }
            for index, text, region, source in _filled(conditions)
        ],
        "issues": [
            {
                "asset_ids": [
                    _asset(index.strip(), images) for index in str(indices).split(",")
                ]
                if str(indices or "").strip()
                else None,
                "message": str(message or "").strip(),
            }
            for indices, message in _filled(issues)
        ],
        "issue_resolutions": [
            {
                "issue_id": str(identity or "").strip(),
                "resolution": RESOLUTION_LABELS.get(
                    str(resolution or "").strip(), str(resolution or "").strip()
                ),
                "reason": str(reason or "").strip(),
            }
            for identity, resolution, reason in _filled(resolutions)
        ],
        "explanation": str(explanation or "").strip(),
    }
    command = ImageManualCheckRequest.model_validate(payload)
    if any(
        finding.finding not in FINDINGS or not finding.reason.strip()
        for finding in command.image_findings
    ):
        raise ValueError("请逐图选择核对结论并填写真实理由。")
    if not command.explanation.strip() or any(
        not item.text.strip() for item in command.confirmed_conditions
    ):
        raise ValueError("核对说明和已确认条件不能空白。")
    return command.model_dump(mode="json")


def assessment_summary(view: ImageAssessmentView) -> str:
    text = f"{STATUS_TEXT[view.status]} · 图像修订 {view.context_revision} · 调用轮次 {view.run_no} · 核对轮次 {view.check_no}"
    run = view.current_run
    if run is not None:
        text += (
            f"\n\n当前机器记录：{run.outcome}，开始于 {run.started_at.isoformat()}。"
        )
        if run.provenance.provider_name or run.provenance.model:
            text += f"实际调用：{run.provenance.provider_name or '未知'} / {run.provenance.model or '未知'}。"
        if run.error is not None:
            text += f"\n\n{run.error.code}：{run.error.message}（阶段：{run.error.stage}）。"
    elif view.run_no:
        text += "\n\n已有调用属于旧输入，历史记录保留，不能替代当前核对。"
    if view.current_check is not None:
        text += f"\n\n当前教师核对：{view.current_check.teacher_id}，{view.current_check.checked_at.isoformat()}。"
    elif view.imported_review is not None:
        text += "\n\n关联了原暂存题的真实核对记录。"
    if view.error is not None and (run is None or run.error is None):
        text += f"\n\n{view.error.code}：{view.error.message}。"
    if not view.evidence_readable and view.input_refs is not None:
        text += "\n\n当前原图不可读，请先恢复原图；不能保存可靠确认。"
    return text


@dataclass
class PaperImageReviewView:
    outputs: list[Any]
    reload: Any


def create_paper_image_review_view(import_id, selected, state) -> PaperImageReviewView:
    # Import here keeps the parent editor's error conversion as the shared UI boundary.
    from backend.app.ui.paper_correction_view import ui_errors

    snapshot = gr.State(None)
    with gr.Accordion("整组题图理解与教师核对", open=False, visible=False) as panel:
        gr.Markdown(
            "先保存题目和题图，再读取整组事实。机器结果不能代替教师核对；条件只摘录原图，有来源页时使用来源页像素坐标，无来源页时使用资产原图像素；无法可靠定位时坐标留空。"
        )
        summary = gr.Markdown("")
        originals = gr.HTML("")
        with gr.Row():
            refresh = gr.Button("读取当前题图核对")
            task = gr.Textbox(label="图片理解任务", value="提取题图中作答所需条件")
            understand = gr.Button("理解整组题图")
        with gr.Accordion("真实机器结果与历史核对", open=False):
            history = gr.JSON(label="题图调用与核对历史", value=None)
        check_status = gr.Radio(
            label="整组核对状态",
            choices=[("已确认", "confirmed"), ("仍有未解决问题", "unresolved")],
            value="unresolved",
        )
        findings = gr.Dataframe(
            label="逐图核对结论与理由",
            headers=["图序", "结论", "真实理由"],
            datatype=["str"] * 3,
            type="array",
            column_count=3,
            column_limits=(3, 3),
            row_count=1,
        )
        gr.Markdown(
            "逐图结论填写：条件已确认、无需额外条件、仍未确定。选择无需额外条件时，也须说明依据。"
        )
        conditions = gr.Dataframe(
            label="教师确认的题图条件",
            headers=[
                "图序",
                "原图中的条件",
                "证据坐标（未知留空）",
                "原机器条件 ID（可空）",
            ],
            datatype=["str"] * 4,
            type="array",
            column_count=4,
            column_limits=(4, 4),
            row_count=1,
        )
        issues = gr.Dataframe(
            label="教师发现的新问题",
            headers=["图序（逗号分隔；整组未知留空）", "问题说明"],
            datatype=["str"] * 2,
            type="array",
            column_count=2,
            column_limits=(2, 2),
            row_count=1,
        )
        resolutions = gr.Dataframe(
            label="既有问题逐项处置",
            headers=["问题 ID", "已解决 / 未解决", "真实依据与理由"],
            datatype=["str"] * 3,
            type="array",
            column_count=3,
            column_limits=(3, 3),
            row_count=1,
        )
        explanation = gr.Textbox(label="本次真实教师核对说明", lines=3)
        save = gr.Button("保存教师核对", variant="primary")
        message = gr.Markdown("")
    outputs = [
        snapshot,
        panel,
        task,
        understand,
        save,
        summary,
        originals,
        history,
        check_status,
        findings,
        conditions,
        issues,
        resolutions,
        explanation,
        message,
    ]

    def clear():
        return {
            snapshot: None,
            task: gr.update(value="提取题图中作答所需条件", interactive=False),
            understand: gr.update(interactive=False),
            save: gr.update(interactive=False),
            panel: gr.update(visible=False),
            summary: "",
            originals: "",
            history: gr.update(value=None),
            check_status: gr.update(value="unresolved"),
            findings: gr.update(value=[]),
            conditions: gr.update(value=[]),
            issues: gr.update(value=[]),
            resolutions: gr.update(value=[]),
            explanation: "",
            message: "",
        }

    def render(view, question_id, current_state, *, editable):
        images = view.input_refs.images if view.input_refs is not None else []
        previews = []
        for image in images:
            try:
                html = loaders.asset_image(
                    question_id, str(image.asset_id), current_state
                )
            except FileStorageError as exc:
                html = f"<p>{escape(exc.code)}：{escape(str(exc))}</p>"
            previews.append(
                f"<p>图 {image.image_index} · {escape(str(image.asset_id))}</p>{html}"
            )
        unresolved = {str(item.issue_id): item for item in view.open_issues}
        # Show machine facts, but never pre-fill a teacher's consent or conditions.
        enabled = bool(editable and images)
        return {
            snapshot: view.model_dump(mode="json"),
            task: gr.update(value="提取题图中作答所需条件", interactive=enabled),
            understand: gr.update(interactive=enabled),
            save: gr.update(interactive=enabled),
            panel: gr.update(visible=True),
            summary: assessment_summary(view),
            originals: "".join(previews),
            history: gr.update(
                value=view.assessment.model_dump(mode="json")
                if view.assessment
                else None
            ),
            check_status: gr.update(value="unresolved", interactive=enabled),
            findings: gr.update(
                value=[[str(image.image_index), "仍未确定", ""] for image in images],
                interactive=enabled,
            ),
            conditions: gr.update(value=[], interactive=enabled),
            issues: gr.update(value=[], interactive=enabled),
            resolutions: gr.update(
                value=[[identity, "未解决", ""] for identity in unresolved],
                interactive=enabled,
            ),
            explanation: gr.update(value="", interactive=enabled),
            message: "请逐图核对，并明确处置所有已登记问题。"
            if enabled
            else "本题无可核对题图，或已不允许继续修改。",
        }

    @ui_errors
    def reload(identity, question_id, current_state, *, editable=True):
        if not identity or not question_id:
            return clear()
        paper = loaders.load(identity, current_state)
        question = next((q for q in paper.questions if str(q.id) == question_id), None)
        if question is None:
            raise ValueError("请选择当前导入的题目。")
        allowed = (
            editable
            and paper.status.value == "Pending Review"
            and question.status.value == "Pending Correction"
        )
        return render(
            loaders.image_assessment(question_id, current_state),
            question_id,
            current_state,
            editable=allowed,
        )

    @ui_errors
    def save_image_check(
        identity,
        question_id,
        displayed,
        status,
        image_rows,
        condition_rows,
        issue_rows,
        resolution_rows,
        note,
        current_state,
    ):
        payload = manual_check_payload(
            displayed,
            status,
            image_rows,
            condition_rows,
            issue_rows,
            resolution_rows,
            note,
        )
        if str(ImageAssessmentView.model_validate(displayed).owner_id) != question_id:
            raise ValueError("当前题目已切换，请重新读取再核对。")
        view = loaders.check_images(question_id, payload, current_state)
        result = render(view, question_id, current_state, editable=True)
        result[message] = "已保存本次真实教师核对；入库后仍须完成正式题语义核验。"
        return result

    @ui_errors
    def _raise(error):
        raise error

    async def understand_image_group(identity, question_id, instruction, current_state):
        if not str(instruction or "").strip():
            raise gr.Error("请填写非空的图片理解任务。")
        try:
            view = await loaders.understand_images(
                question_id, instruction.strip(), current_state
            )
            result = render(view, question_id, current_state, editable=True)
            result[message] = (
                "本轮真实记录已保存。请查看机器结果或错误，并逐图进行教师核对。"
            )
            return result
        except (
            FileStorageError,
            PermissionDeniedError,
            AuthenticationError,
            ValidationError,
            ValueError,
            SQLAlchemyError,
            OSError,
        ) as exc:
            _raise(exc)

    refresh.click(reload, inputs=[import_id, selected, state], outputs=outputs)
    understand.click(
        understand_image_group,
        inputs=[import_id, selected, task, state],
        outputs=outputs,
    )
    save.click(
        save_image_check,
        inputs=[
            import_id,
            selected,
            snapshot,
            check_status,
            findings,
            conditions,
            issues,
            resolutions,
            explanation,
            state,
        ],
        outputs=outputs,
    )
    return PaperImageReviewView(outputs, reload)
