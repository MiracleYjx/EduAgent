"""Teacher confirmation of real chapter/section boundaries and independent Chunk tags."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.domain.permissions import PermissionDeniedError
from backend.app.services.auth_service import AuthenticationError
from backend.app.services.course_service import CourseServiceError
from backend.app.services.file_storage_service import FileStorageError
from backend.app.ui import chapter_scope_loaders as loaders


def scope_ui_errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (
            AuthenticationError,
            PermissionDeniedError,
            CourseServiceError,
            FileStorageError,
            TypeError,
            ValueError,
        ) as exc:
            raise gr.Error(str(exc)) from None
        except SQLAlchemyError:
            raise gr.Error("无法读取或保存章节，请稍后重试。") from None

    return wrapped


@dataclass
class ChapterScopeView:
    panel: gr.Accordion
    reset: Callable[[], dict[Any, Any]]


def create_chapter_scope_view(course, knowledge_base, state) -> ChapterScopeView:
    with gr.Accordion("章节定位与知识点核对", open=False) as panel:
        gr.Markdown(
            "先登记课程章节，再对照完整片段核对。自动标题只是候选；同名章节不会自动合并。"
        )
        chapters = gr.State([])
        records = gr.State([])
        with gr.Row():
            refresh = gr.Button("读取章节与教学资料")
            chapter = gr.Dropdown(
                label="维护章节（留空可登记新章节）", choices=[], interactive=True
            )
            new_chapter = gr.Button("登记新章节")
        title = gr.Textbox(label="章节名称")
        sections = gr.Textbox(
            label="小节目录 JSON（连续序号 1..N；未知目录用 []）", value="[]", lines=3
        )
        gr.Markdown("仅改章名保留定位；小节目录变化会清除本章片段定位，必须重新核对。")
        preserve_titles = gr.Checkbox(
            label="仅修正小节标题：确认原顺序和教学含义不变", value=False
        )
        with gr.Row():
            save_chapter = gr.Button("确认保存章节", variant="primary")
            delete_chapter = gr.Button("删除未引用章节")
        documents = gr.Dropdown(label="核对教学资料", choices=[], interactive=True)
        with gr.Row():
            chunk = gr.Dropdown(
                label="核对片段（全部片段）", choices=[], interactive=True
            )
            reread = gr.Button("重新读取片段")
        content = gr.Textbox(label="完整片段正文（只读）", lines=8, interactive=False)
        evidence = gr.Textbox(
            label="原稿定位、标题候选与当前真实核对记录", lines=7, interactive=False
        )
        with gr.Row():
            location_chapter = gr.Dropdown(
                label="片段所属课程章节", choices=[], interactive=True
            )
            section = gr.Dropdown(
                label="小节序号（可留空表示未知）", choices=[], interactive=True
            )
        gr.Markdown(
            "只有完整片段均属于选定章/节时才确认。跨章或跨节片段请先按原稿真实边界重切分。"
        )
        confirm_location = gr.Button("确认完整片段的章/节定位")
        clear_location = gr.Button("清除定位核对（未知）")
        tags = gr.Textbox(
            label="知识点 JSON 数组（[] 表示确认无知识点）", value="[]", lines=3
        )
        with gr.Row():
            confirm_tags = gr.Button("确认片段知识点")
            clear_tags = gr.Button("清除知识点核对（未知）")
        with gr.Accordion("按持久原稿真实边界重切分", open=False):
            gr.Markdown(
                "字符切点以本次读取的清洗原稿段落为准，从 0 开始。切点必须在段落内部且严格递增。重切分会重新计算向量，成功后新片段须重新核对；失败保留旧片段供诊断，资料进入失败状态。"
            )
            show_source = gr.Button("读取清洗原稿与真实字符位置")
            source = gr.Textbox(
                label="清洗原稿段落（全文与 section_index）",
                lines=12,
                interactive=False,
            )
            split_plan = gr.Textbox(
                label='真实切点 JSON，例如 [{"section_index":1,"cut_points":[120]}]',
                value="[]",
                lines=4,
            )
            split_button = gr.Button("按上述真实边界重新处理")
        message = gr.Markdown("选择课程与知识库后读取资料。")

    initial = {
        preserve_titles: False,
        chapters: [],
        records: [],
        title: "",
        sections: "[]",
        content: "",
        evidence: "",
        tags: "[]",
        source: "",
        split_plan: "[]",
        message: "选择课程与知识库后读取资料。",
    }
    dropdowns = (chapter, documents, chunk, location_chapter, section)

    def reset():
        result = {
            component: gr.update(value=value) for component, value in initial.items()
        }
        result.update(
            {component: gr.update(choices=[], value=None) for component in dropdowns}
        )
        return result

    outputs = [*initial, *dropdowns]

    @scope_ui_errors
    def load_context(course_id, kb_id, current_state):
        values = reset()
        if not course_id:
            return values
        chapter_rows, docs = loaders.list_context(course_id, kb_id, current_state)
        choices = [
            (row["title"] + " · " + row["id"][:8], row["id"]) for row in chapter_rows
        ]
        values[chapters] = chapter_rows
        values[chapter] = gr.update(choices=choices, value=None)
        values[location_chapter] = gr.update(choices=choices, value=None)
        values[documents] = gr.update(
            choices=[
                (row.original_filename + " · " + row.status.value, row.id)
                for row in docs
            ],
            value=None,
        )
        values[message] = "章节和资料已读取；请选择资料及完整片段核对。"
        return values

    @scope_ui_errors
    def chapter_fields(identity, rows):
        selected = next((row for row in rows if row["id"] == identity), None)
        return (
            (
                selected["title"],
                json.dumps(selected["sections"], ensure_ascii=False, indent=2),
                False,
            )
            if selected
            else ("", "[]", False)
        )

    def new_fields():
        return gr.update(value=None), "", "[]", False

    @scope_ui_errors
    def save_directory(course_id, identity, name, directory, current_state, title_only):
        if not course_id:
            raise ValueError("请选择课程。")
        result = loaders.save_chapter(
            course_id,
            identity,
            {"title": name, "sections": json.loads(directory)},
            current_state,
            preserve_section_meaning=title_only,
        )
        return "已确认章节 " + result["title"] + "；请重新读取章节与片段。"

    @scope_ui_errors
    def remove_directory(course_id, identity, current_state):
        if not identity:
            raise ValueError("请选择待删除章节。")
        loaders.remove_chapter(course_id, identity, current_state)
        return "已删除未引用章节；请重新读取章节。"

    @scope_ui_errors
    def load_chunks(course_id, kb_id, identity, current_state):
        rows = (
            loaders.chunks(course_id, kb_id, identity, current_state)
            if identity
            else []
        )
        return (
            gr.update(
                choices=[
                    (f"片段 {row['chunk_index']} · {row['id'][:8]}", row["id"])
                    for row in rows
                ],
                value=None,
            ),
            rows,
            "",
            "",
            gr.update(value=None),
            gr.update(choices=[], value=None),
            "[]",
            "",
            "[]",
        )

    @scope_ui_errors
    def section_choices(identity, chapter_rows):
        selected = next((row for row in chapter_rows if row["id"] == identity), None)
        return gr.update(
            choices=[
                (
                    f"{entry['section_order']} · {entry['title']}",
                    str(entry["section_order"]),
                )
                for entry in selected["sections"]
            ]
            if selected
            else [],
            value=None,
        )

    @scope_ui_errors
    def chunk_fields(identity, rows, chapter_rows):
        selected = next((row for row in rows if row["id"] == identity), None)
        if selected is None:
            return (
                "",
                "",
                gr.update(value=None),
                gr.update(choices=[], value=None),
                "[]",
            )
        metadata = selected["metadata"]
        current_tags = (
            metadata.get("knowledge_points") if isinstance(metadata, dict) else None
        )
        options = section_choices(selected["chapter_id"], chapter_rows)
        options["value"] = (
            str(selected["section_order"])
            if selected["section_order"] is not None
            else None
        )
        return (
            selected["content"],
            json.dumps(metadata, ensure_ascii=False, indent=2),
            gr.update(value=selected["chapter_id"]),
            options,
            json.dumps(current_tags, ensure_ascii=False),
        )

    @scope_ui_errors
    def write_scope(
        course_id,
        kb_id,
        doc_id,
        chunk_id,
        chapter_id,
        section_order,
        tag_text,
        current_state,
        kind,
    ):
        if not doc_id or not chunk_id:
            raise ValueError("请选择资料和片段。")
        if kind == "location":
            if not chapter_id:
                raise ValueError("请选择章节，或使用清除定位操作。")
            payload = {
                "chapter_id": chapter_id,
                "section_order": int(section_order) if section_order else None,
            }
        elif kind == "clear_location":
            payload = {"chapter_id": None, "section_order": None}
        elif kind == "tags":
            payload = {"knowledge_points": json.loads(tag_text)}
            if payload["knowledge_points"] is None:
                raise ValueError("确认知识点必须是数组；未知请使用清除操作。")
        else:
            payload = {"knowledge_points": None}
        updated = loaders.confirm(
            course_id, kb_id, doc_id, chunk_id, payload, current_state
        )
        rows = loaders.chunks(course_id, kb_id, doc_id, current_state)
        return (
            rows,
            json.dumps(updated["metadata"], ensure_ascii=False, indent=2),
            "本次核对已持久保存；另一维的已有核对保持原值。",
        )

    @scope_ui_errors
    def read_source(course_id, kb_id, doc_id, current_state):
        if not doc_id:
            raise ValueError("请选择资料。")
        return json.dumps(
            loaders.sources(course_id, kb_id, doc_id, current_state),
            ensure_ascii=False,
            indent=2,
        )

    @scope_ui_errors
    def run_split(course_id, kb_id, doc_id, plan, current_state):
        if not doc_id:
            raise ValueError("请选择资料。")
        payload = json.loads(plan)
        if not isinstance(payload, list):
            raise TypeError("切点计划必须是 JSON 数组。")
        result = loaders.resplit(course_id, kb_id, doc_id, payload, current_state)
        updated = load_context(course_id, kb_id, current_state)
        if result.status.value == "Failed":
            updated[message] = (
                "重切分失败："
                + str(result.error_code)
                + " · "
                + str(result.error_message)
            )
        else:
            updated[message] = (
                f"重切分成功，生成 {result.chunk_count} 个新片段。请重新读取并核对定位和知识点。"
            )
        return updated

    refresh.click(load_context, [course, knowledge_base, state], outputs)
    course.change(load_context, [course, knowledge_base, state], outputs)
    knowledge_base.change(load_context, [course, knowledge_base, state], outputs)
    chapter.change(
        chapter_fields, [chapter, chapters], [title, sections, preserve_titles]
    )
    new_chapter.click(new_fields, outputs=[chapter, title, sections, preserve_titles])
    save_chapter.click(
        save_directory,
        [course, chapter, title, sections, state, preserve_titles],
        message,
    )
    delete_chapter.click(remove_directory, [course, chapter, state], message)
    chunk_outputs = [
        chunk,
        records,
        content,
        evidence,
        location_chapter,
        section,
        tags,
        source,
        split_plan,
    ]
    documents.change(
        load_chunks, [course, knowledge_base, documents, state], chunk_outputs
    )
    reread.click(load_chunks, [course, knowledge_base, documents, state], chunk_outputs)
    chunk.change(
        chunk_fields,
        [chunk, records, chapters],
        [content, evidence, location_chapter, section, tags],
    )
    location_chapter.input(section_choices, [location_chapter, chapters], section)
    for button, kind in [
        (confirm_location, "location"),
        (clear_location, "clear_location"),
        (confirm_tags, "tags"),
        (clear_tags, "clear_tags"),
    ]:

        def action(*args, _kind=kind):
            return write_scope(*args, kind=_kind)

        button.click(
            action,
            [
                course,
                knowledge_base,
                documents,
                chunk,
                location_chapter,
                section,
                tags,
                state,
            ],
            [records, evidence, message],
        )
    show_source.click(read_source, [course, knowledge_base, documents, state], source)
    split_button.click(
        run_split, [course, knowledge_base, documents, split_plan, state], outputs
    )
    return ChapterScopeView(panel=panel, reset=reset)
