"""Side-by-side persisted source and correction editor."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from html import escape
from typing import Any

import gradio as gr
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from backend.app.domain.permissions import PermissionDeniedError
from backend.app.services.auth_service import AuthenticationError
from backend.app.services.file_storage_service import FileStorageError
from backend.app.ui import paper_import_loaders as loaders
from backend.app.ui.layout_view import (
    empty_state,
    feedback,
    status_badge,
    status_banner,
)

QUESTION_STATES = {
    "Pending Correction": "待校正",
    "Extracted": "已提取",
    "Corrected": "已入库",
    "Rejected": "已拒绝",
}
PAPER_STATES = {
    "Uploaded": "已上传",
    "Parsing": "解析原页中",
    "Extracting": "提取题目中",
    "Pending Review": "待教师校正",
    "Ready": "本次导入已处理",
    "Failed": "处理失败",
    "Rejected": "已全部拒绝",
}


def ui_errors(fn):
    @wraps(fn)
    def action(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (FileStorageError, PermissionDeniedError, AuthenticationError) as exc:
            raise gr.Error(str(exc)) from None
        except ValidationError:
            raise gr.Error(
                "输入格式不合法，请检查题型、分值、坐标和关联字段。"
            ) from None
        except ValueError as exc:
            raise gr.Error(str(exc)) from None
        except (SQLAlchemyError, OSError):
            raise gr.Error(
                "读取或保存失败，请刷新后重试；未确认的操作不算成功。"
            ) from None

    return action


def options_from_rows(rows: list[list[Any]], kind: str):
    filled = [row for row in rows or [] if any(str(x or "").strip() for x in row)]
    if kind == "未知 / 无选项":
        return None
    if kind == "文本选项":
        texts = [str(row[1] or "").strip() for row in filled]
        if any(not text for text in texts):
            raise ValueError("文本选项须有非空内容。")
        return texts
    values = {}
    for label, text in filled:
        label, text = str(label or "").strip(), str(text or "").strip()
        if not label or not text or label in values:
            raise ValueError("标签选项须有唯一标签和非空内容。")
        values[label] = text
    return values


def regions_from_rows(rows: list[list[Any]], paper, *, unknown: bool):
    if unknown:
        return None
    pages = {p.page_number: str(p.id) for p in paper.pages}
    result = []
    for row in rows or []:
        if all(x in (None, "") for x in row):
            continue
        number, *coords = row
        if number not in pages or len(coords) != 4:
            raise ValueError("题目区域页号必须来自本次导入。")
        result.append(
            {"source_page_id": pages[number], "bbox": [float(v) for v in coords]}
        )
    return result


def paper_summary(paper) -> str:
    message = f"{PAPER_STATES[paper.status.value]} · 已保存 {paper.parsed_page_count} 页 / 总页数 {paper.page_count if paper.page_count is not None else '未知'} · 已提取 {paper.question_count} 题"
    if paper.error_code:
        message += f"\n\n{paper.error_code}：{paper.error_message}"
    for diagnostic in paper.file_diagnostics:
        message += "\n\n" + diagnostic["message"]
    return status_banner(paper.status, entity="paper_import", detail=message)


@dataclass
class PaperCorrectionView:
    outputs: list[Any]
    reload: Any
    message: gr.Markdown


def correction_preview(question) -> str:
    """Read-only actual fields; absent answers stay absent, options keep JSON order."""
    if question is None:
        return empty_state("尚未提取题目，查看真实进度后再校正。")

    def text(value):
        return (
            escape(str(value))
            if value is not None and value != ""
            else "未知 / 尚未填写"
        )

    options = question.options
    rows = (
        options.items()
        if isinstance(options, dict)
        else enumerate(options or [], start=1)
    )
    option_html = "".join(
        f"<li><b>{text(key)}</b> {text(value)}</li>" for key, value in rows
    )
    return (
        '<section class="edu-paper-preview"><h3>结构化题目详情</h3>'
        f"<p>{text(question.content)}</p><ul>{option_html}</ul>"
        f"<h3>参考答案</h3><p>{text(question.reference_answer)}</p>"
        f"<h3>评分标准</h3><p>{text(question.scoring_rubric)}</p>"
        f"<h3>解析</h3><p>{text(question.analysis)}</p></section>"
    )


def create_paper_correction_view(import_id, state) -> PaperCorrectionView:
    selected = gr.Dropdown(label="待校正题目", choices=[], interactive=True)
    with gr.Row(elem_classes="edu-paper-columns"):
        with gr.Column(scale=1, min_width=360, elem_classes="edu-surface"):
            page_select = gr.Dropdown(label="查看原页", choices=[], interactive=True)
            page_preview = gr.HTML("<p>选择导入记录后查看真实原页。</p>")
            page_facts = gr.Markdown("")
            source_pages = gr.CheckboxGroup(
                label="本题来源页（可跨页）", choices=[], interactive=True
            )
            with gr.Accordion("题目边界（原页像素）", open=False):
                unknown_boundary = gr.Checkbox(
                    label="边界未知，仅保留真实来源页", value=True
                )
                regions = gr.Dataframe(
                    headers=["页号", "左 x", "上 y", "右 x", "下 y"],
                    datatype=["number"] * 5,
                    type="array",
                    column_count=5,
                    column_limits=(5, 5),
                    row_count=1,
                    label="题目区域",
                    interactive=True,
                )
        with gr.Column(scale=1, min_width=400, elem_classes="edu-surface"):
            q_status = gr.Markdown("选择题目后校正；空缺答案和评分标准不会自动补写。")
            detail = gr.HTML(correction_preview(None), elem_id="edu-paper-detail")
            edit = gr.Button(
                "开始校正",
                elem_id="edu-paper-edit",
                elem_classes="edu-icon",
                interactive=False,
            )
            with gr.Column(visible=False, elem_id="edu-paper-editor") as editor:
                with gr.Row():
                    number = gr.Textbox(label="原题号")
                    order = gr.Number(
                        label="导入内题序", precision=0, minimum=1, value=None
                    )
                    q_type = gr.Dropdown(
                        label="题型",
                        choices=[
                            ("单选题", "SINGLE_CHOICE"),
                            ("判断题", "TRUE_FALSE"),
                            ("简答题", "SHORT_ANSWER"),
                        ],
                        interactive=True,
                    )
                content = gr.Textbox(label="题干", lines=5)
                option_kind = gr.Radio(
                    label="选项形式",
                    choices=["未知 / 无选项", "标签选项", "文本选项"],
                    value="未知 / 无选项",
                )
                options = gr.Dataframe(
                    headers=["标签", "选项内容（保持行顺序）"],
                    datatype=["str", "str"],
                    type="array",
                    column_count=2,
                    column_limits=(2, 2),
                    row_count=1,
                    label="选项",
                    interactive=True,
                )
                with gr.Row():
                    score = gr.Textbox(
                        label="分值", placeholder="确认入库前必填，如 5.00"
                    )
                    points = gr.Textbox(label="知识点（每行一个）", lines=2)
                answer = gr.Textbox(label="参考答案（未知留空）", lines=2)
                rubric = gr.Textbox(label="评分标准（未知留空）", lines=2)
                analysis = gr.Textbox(label="解析（未知留空）", lines=3)
                notes = gr.Textbox(label="校正说明 / 拒绝理由", lines=2)
                no_assets = gr.Checkbox(label="已核对，本题不关联题图", value=False)
                with gr.Row():
                    save = gr.Button(
                        "保存校正",
                        variant="primary",
                        elem_id="edu-paper-save",
                        elem_classes="edu-icon",
                    )
                    cancel = gr.Button(
                        "取消校正", elem_id="edu-paper-cancel", elem_classes="edu-icon"
                    )
                    reject = gr.Button("拒绝此题", variant="stop")
    with gr.Accordion("题图关联与学生展示", open=False):
        gr.Markdown(
            "先保存本题来源页，再裁取或关联题图。整页原图仅教师可读；学生展示须逐图核对。"
        )
        with gr.Row():
            asset_select = gr.Dropdown(label="本题题图", choices=[], interactive=True)
            asset_kind = gr.Dropdown(
                label="图像类型",
                choices=[("图示", "figure"), ("表格", "table"), ("示意图", "diagram")],
                value="figure",
            )
            asset_order = gr.Number(
                label="题图顺序", minimum=1, maximum=5, precision=0, value=1
            )
        asset_preview = gr.HTML("")
        caption = gr.Textbox(label="题图说明")
        visible = gr.Checkbox(label="学生可见（已核对不含答案）", value=False)
        with gr.Row():
            asset_page = gr.Dropdown(label="题图来源页", choices=[], interactive=True)
            crop = gr.Textbox(label="裁图坐标 x0,y0,x1,y1（空白关联整页）")
        with gr.Row():
            add_asset = gr.Button("新增裁图 / 原页关联")
            save_asset = gr.Button("保存题图说明、顺序与展示开关")
            delete_asset = gr.Button("移除此题图")
    from backend.app.ui.paper_image_review_view import create_paper_image_review_view

    image_review = create_paper_image_review_view(import_id, selected, state)
    batch = gr.CheckboxGroup(label="确认入库的题目", choices=[], interactive=True)
    confirm = gr.Button(
        "确认所选题入库",
        variant="primary",
        elem_id="edu-paper-confirm",
        elem_classes="edu-icon",
    )
    message = gr.Markdown("入库后为题库草稿，仍须补全与审核。")
    outputs = [
        detail,
        edit,
        editor,
        selected,
        page_select,
        page_preview,
        page_facts,
        source_pages,
        unknown_boundary,
        regions,
        q_status,
        number,
        order,
        q_type,
        content,
        option_kind,
        options,
        score,
        points,
        answer,
        rubric,
        analysis,
        notes,
        no_assets,
        asset_select,
        asset_preview,
        asset_kind,
        asset_order,
        caption,
        visible,
        asset_page,
        crop,
        batch,
        message,
    ]

    outputs.extend(image_review.outputs)

    def _question(identity, qid, current_state):
        paper = loaders.load(identity, current_state)
        question = next((q for q in paper.questions if str(q.id) == qid), None)
        if question is None:
            raise ValueError("请选择当前导入的题目。")
        return paper, question

    @ui_errors
    def reload(identity, qid, current_state):
        if not identity:
            cleared = {}
            for component in outputs:
                if component in image_review.outputs:
                    continue
                if isinstance(component, gr.Dropdown):
                    cleared[component] = gr.update(
                        choices=(
                            []
                            if component
                            in (selected, page_select, asset_select, asset_page)
                            else component.choices
                        ),
                        value=None,
                    )
                elif isinstance(component, gr.CheckboxGroup):
                    cleared[component] = gr.update(choices=[], value=[])
                elif isinstance(component, gr.Dataframe):
                    cleared[component] = gr.update(value=[])
                elif isinstance(component, gr.Checkbox):
                    cleared[component] = gr.update(value=False)
                elif isinstance(component, gr.Number):
                    cleared[component] = gr.update(value=None)
                elif component is option_kind:
                    cleared[component] = gr.update(value="未知 / 无选项")
                else:
                    cleared[component] = gr.update(value="")
            cleared.update(image_review.reload(None, None, {}))
            cleared[detail] = correction_preview(None)
            cleared[edit] = gr.update(interactive=False, visible=True)
            cleared[editor] = gr.update(visible=False)
            cleared[message] = empty_state("尚未选择导入记录。")
            return cleared
        paper = loaders.load(identity, current_state)
        choices = [
            (
                f"{q.order_index or '?'} · 原题号 {q.question_number or '未知'} · {QUESTION_STATES[q.status.value]}",
                str(q.id),
            )
            for q in paper.questions
        ]
        q = next(
            (q for q in paper.questions if str(q.id) == qid),
            paper.questions[0] if paper.questions else None,
        )
        pages = [(f"第 {p.page_number} 页", str(p.id)) for p in paper.pages]
        p = next(
            (
                p
                for p in paper.pages
                if q and str(p.id) in {str(x) for x in q.source_page_ids}
            ),
            paper.pages[0] if paper.pages else None,
        )
        editable = bool(
            q
            and q.status.value == "Pending Correction"
            and paper.status.value == "Pending Review"
        )
        result = {
            detail: gr.update(value=correction_preview(q), visible=True),
            edit: gr.update(interactive=editable, visible=True),
            editor: gr.update(visible=False),
            selected: gr.update(choices=choices, value=str(q.id) if q else None),
            page_select: gr.update(choices=pages, value=str(p.id) if p else None),
            page_preview: (
                loaders.page_image(identity, str(p.id), current_state)
                if p
                and not any(d["file_id"] == p.file_id for d in paper.file_diagnostics)
                else "<p>原页尚未产生或文件缺失。</p>"
            ),
            page_facts: (
                f"原页 {p.width}×{p.height} 像素 · "
                + ("OCR 已执行" if p.ocr_text is not None else "未执行 OCR")
                if p
                else ""
            ),
            source_pages: gr.update(
                choices=pages,
                value=[str(x) for x in q.source_page_ids] if q else [],
                interactive=editable,
            ),
            batch: gr.update(
                choices=[
                    (label, value)
                    for (label, value), item in zip(
                        choices, paper.questions, strict=True
                    )
                    if item.status.value in {"Pending Correction", "Corrected"}
                ],
                value=[],
            ),
            message: paper_summary(paper),
        }
        text_fields = {
            number: "question_number",
            content: "content",
            score: "score",
            answer: "reference_answer",
            rubric: "scoring_rubric",
            analysis: "analysis",
            notes: "correction_notes",
        }
        for component, name in text_fields.items():
            result[component] = gr.update(
                value=str(getattr(q, name) or "") if q else "", interactive=editable
            )
        opts = q.options if q else None
        result.update(
            {
                order: gr.update(
                    value=q.order_index if q else None, interactive=editable
                ),
                q_type: gr.update(
                    value=q.question_type.value if q and q.question_type else None,
                    interactive=editable,
                ),
                q_status: (
                    (
                        "状态："
                        + status_badge(q.status, entity="extracted_question")
                        + (" · 已创建正式题" if q.question_id else "")
                        + (
                            " · 原始选项顺序无法证明，请对照原卷核对；修改其他字段不会清除此提示。"
                            if not q.order_preserved
                            else ""
                        )
                        + (
                            " · 答案 / 评分标准待补全"
                            if not q.reference_answer or not q.scoring_rubric
                            else ""
                        )
                        + (
                            " · 题图关联待核对"
                            if q.assets is None
                            else " · 题图核对状态见下方" if q.assets else ""
                        )
                    )
                    if q
                    else "本次导入尚未产生题目。"
                ),
                points: gr.update(
                    value="\n".join(q.knowledge_points or []) if q else "",
                    interactive=editable,
                ),
                option_kind: gr.update(
                    value=(
                        "标签选项"
                        if isinstance(opts, dict)
                        else "文本选项" if isinstance(opts, list) else "未知 / 无选项"
                    ),
                    interactive=editable,
                ),
                options: gr.update(
                    value=(
                        list(map(list, opts.items()))
                        if isinstance(opts, dict)
                        else [["", v] for v in opts] if isinstance(opts, list) else []
                    ),
                    interactive=editable,
                ),
                unknown_boundary: gr.update(
                    value=q.source_regions is None if q else True, interactive=editable
                ),
                regions: gr.update(
                    value=(
                        [
                            [
                                next(
                                    p.page_number
                                    for p in paper.pages
                                    if p.id == r.source_page_id
                                ),
                                *r.bbox,
                            ]
                            for r in q.source_regions or []
                        ]
                        if q
                        else []
                    ),
                    interactive=editable,
                ),
                no_assets: gr.update(
                    value=q.assets == [] if q else False, interactive=editable
                ),
                asset_select: gr.update(
                    choices=(
                        [
                            (f"{i} · {a.caption or a.asset_type}", str(a.id))
                            for i, a in enumerate(q.assets or [], 1)
                        ]
                        if q
                        else []
                    ),
                    value=None,
                ),
                asset_preview: "",
                caption: "",
                visible: gr.update(value=False, interactive=editable),
                asset_kind: gr.update(value="figure", interactive=editable),
                asset_order: 1,
                asset_page: gr.update(
                    choices=[
                        (label, value)
                        for label, value in pages
                        if q and value in {str(x) for x in q.source_page_ids}
                    ],
                    value=None,
                ),
                crop: "",
            }
        )
        result.update(
            image_review.reload(
                identity,
                str(q.id) if q else None,
                current_state,
                editable=editable,
                loaded_paper=paper,
            )
        )
        return result

    @ui_errors
    def save_fields(
        identity,
        qid,
        qnumber,
        qorder,
        kind,
        body,
        option_mode,
        rows,
        value,
        knowledge,
        reference,
        grading,
        explanation,
        note,
        pages,
        bounds_unknown,
        bounds,
        no_images,
        current_state,
    ):
        paper, q = _question(identity, qid, current_state)
        payload = {
            "question_number": qnumber or None,
            "order_index": qorder,
            "question_type": kind,
            "content": body or None,
            "options": options_from_rows(rows, option_mode),
            "score": value or None,
            "knowledge_points": (
                [
                    line.strip()
                    for line in (knowledge or "").splitlines()
                    if line.strip()
                ]
                if knowledge
                else ([] if q.knowledge_points is not None else None)
            ),
            "reference_answer": reference or None,
            "scoring_rubric": grading or None,
            "analysis": explanation or None,
            "correction_notes": note or None,
            "source_page_ids": pages,
            "source_regions": regions_from_rows(bounds, paper, unknown=bounds_unknown),
        }
        if no_images:
            if q.assets:
                raise ValueError("请先移除已关联题图，再确认本题无图。")
            payload["assets"] = []
        elif q.assets == []:
            payload["assets"] = None
        loaders.patch(identity, qid, payload, current_state)
        result = reload(identity, qid, current_state)
        result[message] = feedback("校正已保存；重新打开可读取。", "success")
        return result

    @ui_errors
    def reject_question(identity, qid, note, current_state):
        loaders.patch(
            identity, qid, {"action": "reject", "correction_notes": note}, current_state
        )
        return reload(identity, qid, current_state)

    @ui_errors
    def commit_batch(identity, ids, current_state):
        response = loaders.commit(identity, ids, current_state)
        result = reload(identity, None, current_state)
        needs = sum(
            q.completion_status == "needs_completion" for q in response.questions
        )
        result[message] = (
            f"已确认 {len(response.questions)} 题；其中 {needs} 题待补全。所有新题均为草稿，须继续审核。"
        )
        return result

    @ui_errors
    def show_asset(identity, qid, aid, current_state):
        _, q = _question(identity, qid, current_state)
        a = next((a for a in q.assets or [] if str(a.id) == aid), None)
        if a is None:
            return "", "figure", 1, "", False
        return (
            loaders.asset_image(qid, aid, current_state),
            a.asset_type,
            (q.assets or []).index(a) + 1,
            a.caption or "",
            a.student_visible,
        )

    @ui_errors
    def add_image(
        identity, qid, pid, coordinates, kind, note, student_visible, current_state
    ):
        paper, _ = _question(identity, qid, current_state)
        page = next((p for p in paper.pages if str(p.id) == pid), None)
        if page is None:
            raise ValueError("请选择题图来源页。")
        box = (
            [float(x.strip()) for x in coordinates.split(",")] if coordinates else None
        )
        loaders.add_asset(
            qid,
            {
                "file_id": page.file_id,
                "source_page_id": pid,
                "asset_type": kind,
                "region": {"bbox": box} if box else None,
                "caption": note or None,
                "student_visible": student_visible,
            },
            current_state,
        )
        return reload(identity, qid, current_state)

    @ui_errors
    def update_image(
        identity, qid, aid, kind, position, note, student_visible, current_state
    ):
        _, q = _question(identity, qid, current_state)
        assets = [a.model_dump(mode="json") for a in q.assets or []]
        item = next((a for a in assets if a["id"] == aid), None)
        if item is None:
            raise ValueError("请选择本题题图。")
        if not isinstance(position, int) or not 1 <= position <= len(assets):
            raise ValueError("题图顺序超出本题范围。")
        item.update(
            asset_type=kind, caption=note or None, student_visible=student_visible
        )
        assets.remove(item)
        assets.insert(position - 1, item)
        loaders.patch(identity, qid, {"assets": assets}, current_state)
        return reload(identity, qid, current_state)

    @ui_errors
    def remove_image(identity, qid, aid, current_state):
        loaders.remove_asset(qid, aid, current_state)
        return reload(identity, qid, current_state)

    @ui_errors
    def show_page(identity, pid, current_state):
        if not identity or not pid:
            return "", ""
        return loaders.page_preview(identity, pid, current_state)

    @ui_errors
    def begin_edit(identity, qid, current_state):
        result = reload(identity, qid, current_state)
        if not result[edit].get("interactive"):
            raise gr.Error("当前题目不可校正，请重新读取实际状态。")
        result[detail] = gr.update(visible=False)
        result[editor] = gr.update(visible=True)
        return result

    @ui_errors
    def cancel_edit(identity, qid, current_state):
        result = reload(identity, qid, current_state)
        result[message] = feedback("已取消本次校正，重新读取已保存内容。")
        return result

    edit.click(begin_edit, inputs=[import_id, selected, state], outputs=outputs)
    cancel.click(cancel_edit, inputs=[import_id, selected, state], outputs=outputs)

    # Single-select input also fires on blur in Gradio; value changes run once
    # for both mouse and keyboard navigation, while explicit reloads stay current.
    selected.change(reload, inputs=[import_id, selected, state], outputs=outputs)
    page_select.change(
        show_page,
        inputs=[import_id, page_select, state],
        outputs=[page_preview, page_facts],
    )
    save.click(
        save_fields,
        inputs=[
            import_id,
            selected,
            number,
            order,
            q_type,
            content,
            option_kind,
            options,
            score,
            points,
            answer,
            rubric,
            analysis,
            notes,
            source_pages,
            unknown_boundary,
            regions,
            no_assets,
            state,
        ],
        outputs=outputs,
    )
    reject.click(
        reject_question, inputs=[import_id, selected, notes, state], outputs=outputs
    )
    confirm.click(commit_batch, inputs=[import_id, batch, state], outputs=outputs)
    asset_select.input(
        show_asset,
        inputs=[import_id, selected, asset_select, state],
        outputs=[asset_preview, asset_kind, asset_order, caption, visible],
    )
    add_asset.click(
        add_image,
        inputs=[
            import_id,
            selected,
            asset_page,
            crop,
            asset_kind,
            caption,
            visible,
            state,
        ],
        outputs=outputs,
    )
    save_asset.click(
        update_image,
        inputs=[
            import_id,
            selected,
            asset_select,
            asset_kind,
            asset_order,
            caption,
            visible,
            state,
        ],
        outputs=outputs,
    )
    delete_asset.click(
        remove_image, inputs=[import_id, selected, asset_select, state], outputs=outputs
    )
    return PaperCorrectionView(outputs, reload, message)
