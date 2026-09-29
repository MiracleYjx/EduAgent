"""T086: real registered Gradio callbacks over authorized Benchmark records."""

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import gradio as gr
from gradio.blocks import SessionState

from backend.app.services.evaluation_service import EvaluationService
from backend.app.ui.evaluation_dashboard import create_evaluation_view
from backend.app.ui.gradio_app import create_gradio_app


def _record(
    directory: Path, *, run_id: str, mode: str, model: str = "embedding-a",
    status: str = "ok", evidence: str = "provider_run",
) -> None:
    payload = {
        "run_id": run_id, "run_at": "2026-09-20T12:00:00+00:00",
        "config": mode, "dataset_version": "same-dataset", "model_version": model,
        "prompt_version": "prompt-v1", "status": status,
        "error_code": "EMBEDDING_PROVIDER_NOT_READY" if status == "failed" else None,
        "environment": {"top_k": 10},
        "metrics": {"recall_at_5": 0.8 if mode == "hybrid" else 0.6,
                    "latency_p95_ms": 12.5},
        "results": [{"query_id": "q1"}, {"query_id": "q2"}],
        "reproducibility": {
            "evidence_kind": evidence,
            "comparison": {"input_fingerprint": "same-input", "query_ids": ["q1", "q2"]},
        },
    }
    (directory / f"retrieval_{run_id}_{mode}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _app(directory: Path, actor: UUID) -> gr.Blocks:
    service = EvaluationService(directory, authorize=lambda current: current == actor)
    with gr.Blocks() as app:
        create_evaluation_view(
            service=service, actor_id=actor, read_authorized=True, visible=True,
        )
    return app


def _callback(app: gr.Blocks, name: str):
    matches = [
        block for block in app.fns.values()
        if block.fn is not None and getattr(block.fn, "__name__", "") == name
    ]
    assert len(matches) == (5 if name == "refresh" else 1)
    assert len({id(block.fn) for block in matches}) == 1
    return matches[0]


def test_registered_filter_refresh_queries_service_and_keeps_chart_consistent(tmp_path: Path) -> None:
    actor = uuid4()
    _record(tmp_path, run_id="run-a", mode="hybrid")
    app = _app(tmp_path, actor)
    refresh = _callback(app, "refresh")
    assert [control.label for control in refresh.inputs[:4]] == [
        "实验", "数据集", "模型", "检索模式",
    ]
    # A new file after view construction must be loaded by the registered event.
    _record(tmp_path, run_id="run-a", mode="keyword_only")
    response = asyncio.run(app.process_api(
        refresh, ["retrieval/run-a", "same-dataset", "embedding-a", "", "0"],
        state=SessionState(app),
    ))
    rows = response["data"][0]
    chart = response["data"][2]
    note = response["data"][3]
    assert any(row[4] == "keyword_only" for row in rows["data"])
    assert "比例" in note and "样本量 2" in note and "同语料" in note
    table_values = {float(row[6]) for row in rows["data"] if row[5] == "recall_at_5"}
    value_column = chart["columns"].index("指标值")
    chart_values = {float(item[value_column]) for item in chart["data"]}
    assert chart_values == table_values == {0.6, 0.8}

    selected = _callback(app, "select_metric")
    event = gr.EventData(None, {"index": (0, 0), "selected": True, "value": None})
    detail = asyncio.run(app.process_api(
        selected, ["retrieval/run-a", "same-dataset", "embedding-a", ""],
        state=SessionState(app), event_data=event,
    ))
    assert any("Top-K: 10" in value for value in detail["data"])
    assert any("retrieval_run-a_hybrid.json" in value for value in detail["data"])


def test_registered_refresh_separates_failures_selftest_and_empty_state(tmp_path: Path) -> None:
    actor = uuid4()
    _record(tmp_path, run_id="run-f", mode="vector_only", status="failed")
    _record(tmp_path, run_id="run-s", mode="hybrid", model="stub-model",
            evidence="pipeline_selftest")
    app = _app(tmp_path, actor)
    refresh = _callback(app, "refresh")
    failed = refresh.fn("retrieval/run-f", "", "", "vector_only", "")
    assert failed[0] == []
    assert len(failed[4]) == 1
    assert failed[4][0][4] == "EMBEDDING_PROVIDER_NOT_READY"
    selftest = refresh.fn("retrieval/run-s", "", "stub-model", "hybrid", "")
    assert any("管道自检" in row[10] for row in selftest[0])
    assert selftest[2] is None
    empty = refresh.fn("retrieval/run-f", "missing", "", "", "")
    assert empty[0] == [] and "无数据" in empty[1]


def test_ungranted_actor_gets_no_data_even_if_view_flag_is_true(tmp_path: Path) -> None:
    actor = uuid4()
    _record(tmp_path, run_id="run-a", mode="hybrid")
    service = EvaluationService(tmp_path, authorize=lambda current: current == actor)
    with gr.Blocks() as app:
        create_evaluation_view(
            service=service, actor_id=uuid4(), read_authorized=True, visible=True,
        )
    refresh = _callback(app, "refresh")
    output = refresh.fn("", "", "", "", "")
    assert output[0] == []
    assert output[2] is None


def test_missing_metric_is_rendered_as_no_data(tmp_path: Path) -> None:
    actor = uuid4()
    _record(tmp_path, run_id="run-m", mode="hybrid")
    result_file = tmp_path / "retrieval_run-m_hybrid.json"
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    payload["metrics"].pop("recall_at_5")
    result_file.write_text(json.dumps(payload), encoding="utf-8")
    refresh = _callback(_app(tmp_path, actor), "refresh")
    rows = refresh.fn("", "", "", "", "")[0]
    assert any(row[5] == "recall_at_5" and row[6] == "无数据" for row in rows)


def test_production_workspace_keeps_evaluation_entry_hidden_without_grant() -> None:
    app = create_gradio_app()
    panels = [
        block for block in app.blocks.values()
        if getattr(block, "elem_id", None) == "edu-evaluation"
    ]
    assert len(panels) == 1
    assert panels[0].visible is False
