"""T182 read-only presentation of service statistics and actual answer records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from html import escape
from typing import Any

DISTRIBUTION_HEADERS = ("实际最终分值", "最终答卷数")
QUESTION_STAT_HEADERS = (
    "题号",
    "本场关联",
    "本场满分",
    "发布知识点",
    "有效最终答卷",
    "排除最终答卷",
    "得分分子",
    "满分分母",
    "失分",
    "得分率",
    "观测说明",
)
KNOWLEDGE_STAT_HEADERS = (
    "知识点",
    "本场关联",
    "有效学生",
    "有效答案",
    "得分分子",
    "满分分母",
    "失分",
    "得分率",
)
ATTENTION_HEADERS = ("学生ID", "答卷ID", "关注原因", "最终失分", "错误码", "相关答案")
ANSWER_DETAIL_HEADERS = (
    "题号",
    "实际答案",
    "处理状态",
    "得分",
    "本场满分",
    "评分解释",
    "答案ID",
)


def value(item: Any, name: str, default: Any = None) -> Any:
    return (
        item.get(name, default)
        if isinstance(item, Mapping)
        else getattr(item, name, default)
    )


def text(item: Any, missing: str = "暂无数据") -> str:
    return missing if item is None or item == "" else str(getattr(item, "value", item))


def labels(items: Any, missing: str = "未知（待核对）") -> str:
    return missing if items is None else "、".join(str(i) for i in items) or "未标注"


def teacher_analysis_values(summary: Any | None) -> tuple[Any, ...]:
    if summary is None:
        return "暂无考情数据；选择课程与考试后刷新。", [], [], [], [], []
    fields = (
        ("可参加", "eligible_count"),
        ("已参加", "participated_count"),
        ("草稿", "draft_count"),
        ("未开始", "not_participated_count"),
        ("最终答卷", "final_count"),
        ("待复核答卷", "pending_review_submission_count"),
        ("处理失败", "failed_submission_count"),
        ("依据不足", "insufficient_evidence_submission_count"),
        ("尚未完成", "unfinished_submission_count"),
    )
    overview = " · ".join(
        f"{name}：{escape(text(value(summary, field), '暂无'))}"
        for name, field in fields
    )
    overview += "\n\n分布与逐题统计只使用当前最终答卷；得分率的真实分子/分母列在下表。多标签知识点不能加总成整卷失分；依据不足可与最终历史答卷重叠。"
    distribution = [
        [text(value(i, "score")), text(value(i, "submission_count"))]
        for i in value(summary, "final_score_distribution", [])
    ]
    if not distribution:
        overview += "\n\n暂无最终分数分布；未完成、失败和待复核不按零分计入。"
    questions = [
        [
            text(value(i, "order")),
            text(value(i, "exam_question_id"), "未知（待核对）"),
            text(value(i, "max_score"), "未知（待核对）"),
            labels(value(i, "published_knowledge_points")),
            *[
                text(value(i, f))
                for f in (
                    "effective_submission_count",
                    "excluded_final_submission_count",
                    "awarded_score",
                    "maximum_score",
                    "lost_score",
                    "score_rate",
                )
            ],
            text(
                value(i, "not_ready_reason"),
                (
                    "已有最终观测"
                    if value(i, "effective_submission_count", 0)
                    else "暂无最终观测"
                ),
            ),
        ]
        for i in value(summary, "question_statistics", [])
    ]
    points = [
        [
            text(value(i, "knowledge_point")),
            labels(value(i, "exam_question_ids")),
            *[
                text(value(i, f))
                for f in (
                    "effective_student_count",
                    "effective_answer_count",
                    "awarded_score",
                    "maximum_score",
                    "lost_score",
                    "score_rate",
                )
            ],
        ]
        for i in value(summary, "knowledge_point_statistics", [])
    ]
    records = [
        i.model_dump(mode="python") if hasattr(i, "model_dump") else dict(i)
        for i in value(summary, "attention_students", [])
    ]
    attention = [
        [
            text(i.get("student_id")),
            text(i.get("submission_id"), "尚无答卷"),
            "；".join(i.get("reasons", [])),
            text(i.get("final_lost_score"), "暂无最终失分"),
            text(i.get("error_code"), "无"),
            labels(i.get("answer_ids", []), "未知"),
        ]
        for i in records
    ]
    return overview, distribution, questions, points, attention, records


def answer_text(content: Any) -> str:
    return (
        text(content, "未作答/未知")
        if content is None or isinstance(content, str)
        else json.dumps(content, ensure_ascii=False, sort_keys=False)
    )


def teacher_answer_rows(detail: Any | None) -> list[list[str]]:
    if detail is None:
        return []
    answers = value(detail, "answers", {})
    rows = [
        [
            text(value(i, "order")),
            answer_text(answers.get(value(i, "answer_id"))),
            text(value(i, "grading_status")),
            text(value(i, "effective_score"), "尚未确认"),
            text(value(i, "max_score"), "未知（待核对）"),
            text(value(i, "reason"), "暂无评分解释"),
            text(value(i, "answer_id")),
        ]
        for i in value(value(detail, "result"), "items", [])
    ]
    known = {value(i, "answer_id") for i in value(value(detail, "result"), "items", [])}
    for item in value(detail, "answer_metadata", []):
        if item["answer_id"] not in known:
            rows.append(
                [
                    text(item["order"], "未知题序"),
                    answer_text(answers[item["answer_id"]]),
                    text(item["status"]),
                    "尚无确认评分",
                    "未知（待核对）",
                    "尚无评分解释",
                    item["answer_id"],
                ]
            )
    return rows
