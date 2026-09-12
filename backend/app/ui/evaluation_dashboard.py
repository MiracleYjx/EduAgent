"""评测看板只读 Gradio 视图。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Literal, cast

import gradio as gr
import pandas as pd

from backend.app.ui.layout_view import table_options

EVALUATION_NOT_READY = "评测数据暂未就绪，请等待 M5 阶段完成。"
NO_DATA = "无数据"
METRIC_HEADERS = (
    "实验",
    "模型",
    "提示词",
    "数据集",
    "检索模式",
    "指标",
    "指标值",
    "单位",
    "样本量",
    "运行状态",
    "结果位置",
)
FAILURE_HEADERS = ("实验", "模型", "数据集", "运行状态", "失败原因", "结果位置")
METRIC_DATATYPES = cast(tuple[Literal["str"], ...], ("str",) * len(METRIC_HEADERS))
FAILURE_DATATYPES = cast(tuple[Literal["str"], ...], ("str",) * len(FAILURE_HEADERS))
COMPARISON_NOTE = (
    "仅比较相同数据集及版本、指标单位、样本量和已记录比较条件的完成实验；"
    "运行中、失败或比较信息缺失的结果不参与柱状图。"
)
DETAIL_LABELS = (
    "选中实验 / 运行标识",
    "配置",
    "模型 / 模型版本",
    "提示词 / 提示词版本",
    "数据集 / 数据集版本",
    "检索模式",
    "运行状态",
    "运行时间",
    "结果位置",
    "失败原因",
)


@dataclass(frozen=True)
class EvaluationRecord:
    """界面只读投影；由后续 T086 适配已授权、已脱敏的结果。"""

    run_id: str
    experiment: str
    model: str
    prompt: str
    dataset: str
    retrieval_mode: str
    status: str
    metrics: Mapping[str, float | int | None] = field(default_factory=dict)
    metric_units: Mapping[str, str] = field(default_factory=dict)
    sample_count: int | None = None
    model_version: str = ""
    prompt_version: str = ""
    dataset_version: str = ""
    configuration: str = ""
    run_at: str = ""
    result_path: str = ""
    failure_reason: str = ""
    comparison_condition: str = ""


@dataclass(frozen=True)
class EvaluationView:
    """供主工作台隐藏挂载的评测组件集合。"""

    panel: gr.Column
    filters: tuple[gr.Dropdown, ...]
    metrics_table: gr.Dataframe
    chart: gr.BarPlot
    chart_filter: gr.Dropdown
    failures_table: gr.Dataframe
    details: tuple[gr.Textbox, ...]


@dataclass(frozen=True)
class _ChartGroup:
    """比较分组只使用上游已记录的条件，不推测缺失信息。"""

    metric: str
    unit: str
    dataset: str
    dataset_version: str
    sample_count: int
    condition: str

    @property
    def label(self) -> str:
        return (
            f"{self.metric}（{self.unit}） / {self.dataset} {self.dataset_version} / "
            f"样本量 {self.sample_count} / {self.condition}"
        )


def _status_label(status: str) -> str:
    return {
        "running": "运行中",
        "completed": "已完成",
        "failed": "失败",
    }.get(status.strip().lower(), "状态未知")


def _number_text(value: float | None) -> str:
    if value is None or isinstance(value, bool) or not isfinite(value):
        return NO_DATA
    return str(value)


def _chart_group(record: EvaluationRecord, metric: str) -> _ChartGroup | None:
    count = record.sample_count
    unit = record.metric_units.get(metric, "")
    if (
        _status_label(record.status) != "已完成"
        or _number_text(record.metrics.get(metric)) == NO_DATA
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count <= 0
        or not unit
        or not record.dataset
        or not record.dataset_version
        or not record.comparison_condition
    ):
        return None
    return _ChartGroup(
        metric,
        unit,
        record.dataset,
        record.dataset_version,
        count,
        record.comparison_condition,
    )


def _chart_values(
    records: Sequence[EvaluationRecord], group: _ChartGroup | None
) -> tuple[pd.DataFrame | None, str]:
    if group is None:
        return None, "无数据：缺少指标、单位、样本量或同条件比较信息。"
    rows = [
        {
            "实验": f"{record.experiment} / {record.run_id or NO_DATA}（{index + 1}）",
            "指标值": record.metrics[group.metric],
            "指标": group.metric,
            "单位": group.unit,
            "样本量": group.sample_count,
        }
        for index, record in enumerate(records)
        if _chart_group(record, group.metric) == group
    ]
    if not rows:
        return None, "无数据：当前筛选范围没有符合所选比较条件的完成实验。"
    return pd.DataFrame(rows), group.label


def _filtered_records(
    records: Sequence[EvaluationRecord],
    experiment: str,
    dataset: str,
    model: str,
    retrieval_mode: str,
) -> list[EvaluationRecord]:
    return [
        record
        for record in records
        if (not experiment or record.experiment == experiment)
        and (not dataset or record.dataset == dataset)
        and (not model or record.model == model)
        and (not retrieval_mode or record.retrieval_mode == retrieval_mode)
    ]


def _metric_entries(
    records: Sequence[EvaluationRecord],
) -> list[tuple[EvaluationRecord, str]]:
    names = sorted({name for record in records for name in record.metrics}) or [NO_DATA]
    return [
        (record, name)
        for record in records
        if _status_label(record.status) != "失败"
        for name in names
    ]


def _metric_rows(records: Sequence[EvaluationRecord]) -> list[list[str]]:
    return [
        [
            record.experiment,
            record.model or NO_DATA,
            record.prompt or NO_DATA,
            record.dataset or NO_DATA,
            record.retrieval_mode or NO_DATA,
            name,
            _number_text(record.metrics.get(name)),
            record.metric_units.get(name) or NO_DATA,
            _number_text(record.sample_count),
            _status_label(record.status),
            record.result_path or NO_DATA,
        ]
        for record, name in _metric_entries(records)
    ]


def _failure_rows(records: Sequence[EvaluationRecord]) -> list[list[str]]:
    return [
        [
            record.experiment,
            record.model or NO_DATA,
            record.dataset or NO_DATA,
            "失败",
            record.failure_reason or NO_DATA,
            record.result_path or NO_DATA,
        ]
        for record in records
        if _status_label(record.status) == "失败"
    ]


def _detail_values(record: EvaluationRecord | None) -> tuple[str, ...]:
    if record is None:
        return ("尚未选择实验。", *([NO_DATA] * (len(DETAIL_LABELS) - 1)))
    return (
        f"{record.experiment} / {record.run_id or NO_DATA}",
        record.configuration or NO_DATA,
        f"{record.model or NO_DATA} / {record.model_version or NO_DATA}",
        f"{record.prompt or NO_DATA} / {record.prompt_version or NO_DATA}",
        f"{record.dataset or NO_DATA} / {record.dataset_version or NO_DATA}",
        record.retrieval_mode or NO_DATA,
        _status_label(record.status),
        record.run_at or NO_DATA,
        record.result_path or NO_DATA,
        record.failure_reason or NO_DATA,
    )


def create_evaluation_view(
    records: Sequence[EvaluationRecord] | None = None,
    *,
    read_authorized: bool = False,
    visible: bool = False,
) -> EvaluationView:
    """在已有 Blocks 中展示授权快照；角色名不能代替读取授权结论。"""

    ready = read_authorized is True and records is not None
    source = tuple(records) if ready and records is not None else ()
    groups = list(
        dict.fromkeys(
            group
            for record in source
            for metric in record.metrics
            if (group := _chart_group(record, metric)) is not None
        )
    )
    group_choices = {str(index): group for index, group in enumerate(groups)}
    initial_group = next(iter(group_choices), "")
    chart_data, chart_note = _chart_values(source, group_choices.get(initial_group))
    with gr.Column(visible=visible, elem_id="edu-evaluation") as panel:
        gr.Markdown("## 评测看板")
        gr.Markdown(
            EVALUATION_NOT_READY
            if not ready
            else ("暂无评测记录。" if not source else "当前授权范围内的评测结果。")
        )
        with gr.Column(), gr.Row():
            filters = tuple(
                gr.Dropdown(
                    label=label,
                    choices=[
                        (f"全部{label}", ""),
                        *[
                            (value, value)
                            for value in sorted(
                                {getattr(row, attribute) for row in source}
                            )
                            if value
                        ],
                    ],
                    value="",
                    interactive=bool(source),
                )
                for label, attribute in (
                    ("实验", "experiment"),
                    ("数据集", "dataset"),
                    ("模型", "model"),
                    ("检索模式", "retrieval_mode"),
                )
            )
        with gr.Row():
            with gr.Column(scale=60, min_width=420):
                gr.Markdown("### 指标对比")
                metrics_table = gr.Dataframe(
                    headers=list(METRIC_HEADERS),
                    datatype=METRIC_DATATYPES,
                    value=_metric_rows(source),
                    interactive=False,
                    label="指标对比表",
                    **table_options(METRIC_HEADERS),
                )
                metrics_empty = gr.Markdown(
                    "" if _metric_rows(source) else "无数据：暂无可展示指标。"
                )
            with gr.Column(scale=40, min_width=320):
                gr.Markdown("### 指标柱状图")
                chart_filter = gr.Dropdown(
                    label="指标与比较条件",
                    choices=[
                        ("请选择比较条件", ""),
                        *[(group.label, key) for key, group in group_choices.items()],
                    ],
                    value=initial_group,
                    interactive=bool(groups),
                )
                chart = gr.BarPlot(
                    value=chart_data,
                    x="实验",
                    y="指标值",
                    x_title="实验 / 运行标识",
                    y_title="指标值（单位见比较条件）",
                    label="指标柱状图",
                    height=300,
                    tooltip=["实验", "指标", "指标值", "单位", "样本量"],
                )
                chart_summary = gr.Textbox(
                    label="指标单位 / 样本量 / 同条件比较",
                    value=chart_note,
                    lines=3,
                    interactive=False,
                )
                gr.Markdown(COMPARISON_NOTE)
        with gr.Column():
            gr.Markdown("### 失败项")
            failures_table = gr.Dataframe(
                headers=list(FAILURE_HEADERS),
                datatype=FAILURE_DATATYPES,
                value=_failure_rows(source),
                interactive=False,
                label="失败项",
                **table_options(FAILURE_HEADERS),
            )
            failures_empty = gr.Markdown(
                ""
                if _failure_rows(source)
                else ("暂无失败记录。" if ready else "失败记录暂未就绪。")
            )
        with gr.Accordion("选中实验详情", open=False):
            details = tuple(
                gr.Textbox(label=label, value=value, interactive=False)
                for label, value in zip(
                    DETAIL_LABELS, _detail_values(None), strict=True
                )
            )

        def refresh(
            experiment: str,
            dataset: str,
            model: str,
            retrieval_mode: str,
            comparison: str,
        ) -> tuple[Any, ...]:
            selected = _filtered_records(
                source, experiment, dataset, model, retrieval_mode
            )
            metric_rows = _metric_rows(selected)
            failure_rows = _failure_rows(selected)
            plot, note = _chart_values(selected, group_choices.get(comparison))
            return (
                metric_rows,
                "" if metric_rows else "无数据：暂无可展示指标。",
                plot,
                note,
                failure_rows,
                (
                    ""
                    if failure_rows
                    else ("暂无失败记录。" if ready else "失败记录暂未就绪。")
                ),
                *_detail_values(None),
            )

        def selected_details(
            rows: Sequence[EvaluationRecord], event: gr.SelectData
        ) -> tuple[str, ...]:
            index = (
                event.index[0]
                if isinstance(event.index, (tuple, list))
                else event.index
            )
            if (
                not event.selected
                or not isinstance(index, int)
                or not 0 <= index < len(rows)
            ):
                return _detail_values(None)
            return _detail_values(rows[index])

        def select_metric(
            experiment: str,
            dataset: str,
            model: str,
            retrieval_mode: str,
            event: gr.SelectData,
        ) -> tuple[str, ...]:
            selected = _filtered_records(
                source, experiment, dataset, model, retrieval_mode
            )
            return selected_details(
                [record for record, _ in _metric_entries(selected)], event
            )

        def select_failure(
            experiment: str,
            dataset: str,
            model: str,
            retrieval_mode: str,
            event: gr.SelectData,
        ) -> tuple[str, ...]:
            selected = _filtered_records(
                source, experiment, dataset, model, retrieval_mode
            )
            return selected_details(
                [
                    record
                    for record in selected
                    if _status_label(record.status) == "失败"
                ],
                event,
            )

        all_filters = [*filters, chart_filter]
        outputs = [
            metrics_table,
            metrics_empty,
            chart,
            chart_summary,
            failures_table,
            failures_empty,
            *details,
        ]
        for control in all_filters:
            control.input(refresh, inputs=all_filters, outputs=outputs)
        metrics_table.select(select_metric, inputs=list(filters), outputs=list(details))
        failures_table.select(
            select_failure, inputs=list(filters), outputs=list(details)
        )
    return EvaluationView(
        panel, filters, metrics_table, chart, chart_filter, failures_table, details
    )


def create_evaluation_dashboard(
    records: Sequence[EvaluationRecord] | None = None,
    *,
    read_authorized: bool = False,
) -> gr.Blocks:
    """创建独立预览；未授权时只显示未就绪空壳，不暴露传入记录。"""

    with gr.Blocks(title="EduAgent 评测看板", fill_width=True) as dashboard:
        create_evaluation_view(records, read_authorized=read_authorized, visible=True)
    return dashboard
