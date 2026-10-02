"""Source-HEAD Gradio correction panel with passive browser timing; no model calls."""

from __future__ import annotations

import argparse
import html
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import gradio as gr
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.app.core.config import get_settings
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import UserRole
from backend.app.models import User
from backend.app.services.auth_service import AuthService
from backend.app.ui import paper_import_loaders as loaders
from backend.app.ui.paper_correction_view import create_paper_correction_view


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument(
        "--workload", choices=["text", "figure", "cross"], required=True
    )
    parser.add_argument("--temperature", choices=["cold", "warm"], required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--port", type=int, default=7871)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert "eduagent_e2_acceptance_" in str(get_settings().database_url)
    args.output.mkdir(parents=True, exist_ok=True)
    spec = json.loads(args.fixtures.read_text(encoding="utf-8"))
    with get_session_factory()() as session:
        user = session.get(User, UUID(spec["actor_id"]))
        if user is None:
            raise ValueError("Actual fixture actor does not exist.")
        auth = AuthService(session)
        token = auth.issue_access_token(user)
        actual = auth.get_current_user(token)
        if UserRole.TEACHER not in {r.name for r in actual.roles}:
            raise ValueError("Teacher role required.")
        login_state = {
            "access_token": token,
            "user_id": str(actual.id),
            "roles": ["Teacher"],
        }
    source_rows = [
        r
        for r in spec["instances"]
        if r["workload"] == args.workload and r["temperature"] == args.temperature
    ]
    if args.temperature == "cold":
        source_rows = [r for r in source_rows if r["repeat_index"] == args.repeat]
    if not source_rows:
        raise ValueError("No prepared fresh replay instance for this workload/repeat.")
    descriptors = {r["instance_key"]: r for r in source_rows}
    type_labels = {
        "SINGLE_CHOICE": "\u5355\u9009\u9898",
        "TRUE_FALSE": "\u5224\u65ad\u9898",
        "SHORT_ANSWER": "\u7b80\u7b54\u9898",
    }

    def config_for(row):
        paper = loaders.load(row["paper_import_id"], login_state)
        target = next(
            q for q in paper.questions if str(q.id) == row["target_question_id"]
        )
        if row["action_kind"] == "page":
            page = next(
                p for p in paper.pages if p.page_number == row["target_page_number"]
            )
            label = f"\u7b2c {page.page_number} \u9875"
            selector_id = "t160-page-select"
        else:
            page = next(
                p
                for p in paper.pages
                if str(p.id) in {str(v) for v in target.source_page_ids}
            )
            label = f"{target.order_index or '?'} \u00b7 \u539f\u9898\u53f7 {target.question_number or '\u672a\u77e5'} \u00b7 \u5f85\u6821\u6b63"
            selector_id = "t160-selected"
        fields = {
            "number": target.question_number or "",
            "order": str(target.order_index or ""),
            "qtype": type_labels.get(
                target.question_type.value if target.question_type else "", ""
            ),
            "content": target.content or "",
            "score": str(target.score or ""),
            "points": "\n".join(target.knowledge_points or []),
            "answer": target.reference_answer or "",
            "rubric": target.scoring_rubric or "",
            "analysis": target.analysis or "",
        }
        return {
            "run_id": row["instance_key"],
            "selector_id": selector_id,
            "target_option_label": label,
            "timeout_ms": 15000,
            "screenshot_ref": f"{args.output.name}/screenshots/{row['instance_key']}.png",
            "identity": {
                k: row[k]
                for k in [
                    "case_id",
                    "workload",
                    "temperature",
                    "repeat_index",
                    "warmup",
                    "action_kind",
                    "paper_import_id",
                ]
            },
            "expected": {
                "fields": fields,
                "require_interactive": True,
                "page_alt": f"\u539f\u5377\u7b2c {page.page_number} \u9875\uff0c{page.width}\u00d7{page.height} \u50cf\u7d20",
            },
        }

    def config_html(row):
        cfg = config_for(row)
        return (
            '<div hidden data-t160-config="'
            + html.escape(json.dumps(cfg, ensure_ascii=False), quote=True)
            + '"></div>'
        )

    first = source_rows[0]
    js = (Path(__file__).with_name("correction_observer.js")).read_text(
        encoding="utf-8"
    )
    with gr.Blocks(title="T160 correction timing") as panel:
        gr.Markdown(
            "## T160: actual correction panel / source-backed replay for UI timing"
        )
        gr.Markdown(
            "No new extraction or model calls. Select a fresh prepared instance, wait for initial page, then make the indicated native selection."
        )
        instance = gr.Dropdown(
            label="Acceptance instance (load before timing)",
            choices=[r["instance_key"] for r in source_rows],
            value=first["instance_key"],
            interactive=True,
        )
        observer_config = gr.HTML(config_html(first), elem_id="t160-observer-config")
        gr.HTML(
            '<div id="t160-observer-status">Waiting for an actual target selection.</div>'
        )
        instructions = gr.Markdown("")
        identity = gr.State(first["paper_import_id"])
        login = gr.State(login_state)
        with gr.Column(elem_id="t160-panel"):
            view = create_paper_correction_view(identity, login)
        ids = {
            0: "selected",
            1: "page-select",
            2: "page-preview",
            3: "page-facts",
            8: "number",
            9: "order",
            10: "qtype",
            11: "content",
            13: "options",
            14: "score",
            15: "points",
            16: "answer",
            17: "rubric",
            18: "analysis",
        }
        for index, label in ids.items():
            view.outputs[index].elem_id = "t160-" + label

        def load_instance(key):
            row = descriptors[key]
            result = view.reload(
                row["paper_import_id"], row["initial_question_id"], login_state
            )
            result[identity] = row["paper_import_id"]
            result[observer_config] = config_html(row)
            result[instructions] = (
                "Actual target: **"
                + config_for(row)["target_option_label"]
                + "**; action="
                + row["action_kind"]
                + "; run="
                + row["instance_key"]
            )
            return result

        all_outputs = [*view.outputs, identity, observer_config, instructions]
        panel.load(load_instance, inputs=[instance], outputs=all_outputs)
        instance.input(load_instance, inputs=[instance], outputs=all_outputs)
    service = FastAPI()
    guard = threading.Lock()
    allowed = {
        "run_id",
        "event",
        "status",
        "reason",
        "case_id",
        "workload",
        "temperature",
        "repeat_index",
        "warmup",
        "action_kind",
        "paper_import_id",
        "started_at",
        "ended_at",
        "start_event",
        "end_event",
        "start_perf_ms",
        "end_perf_ms",
        "elapsed_ms",
        "measurement_source",
        "timeout_limit_ms",
        "target_ms",
        "target_met",
        "screenshot_ref",
        "screenshot_status",
        "actual_snapshot",
        "before_snapshot",
        "console_error_observations",
    }

    @service.post("/t160-observer")
    async def receive(request: Request):
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("run_id") not in descriptors:
            raise HTTPException(422)
        clean = {k: v for k, v in data.items() if k in allowed}
        clean.update(
            server_received_at=datetime.now(UTC).isoformat(),
            service_pid=os.getpid(),
            source_kind="source_backed_replay_for_ui_timing",
            duration_excludes_cua_transport=True,
        )
        with (
            guard,
            (args.output / "browser-measurements.jsonl").open(
                "a", encoding="utf-8"
            ) as out,
        ):
            out.write(json.dumps(clean, ensure_ascii=False) + "\n")
            out.flush()
        return JSONResponse({"recorded": True, "run_id": data["run_id"]})

    @service.get("/t160-observer-receipts")
    def receipts():
        path = args.output / "browser-measurements.jsonl"
        return {
            "service_pid": os.getpid(),
            "records": (
                [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
                if path.exists()
                else []
            ),
        }

    @service.post("/t160-screenshot/{run_id}")
    async def capture(run_id: str, request: Request):
        if run_id not in descriptors:
            raise HTTPException(404)
        content = await request.body()
        kind = (
            "png"
            if content.startswith(b"\x89PNG\r\n\x1a\n")
            else "jpg" if content.startswith(b"\xff\xd8\xff") else None
        )
        if len(content) > 8 * 1024 * 1024 or kind is None:
            raise HTTPException(422)
        folder = args.output / "screenshots"
        folder.mkdir(exist_ok=True)
        target = folder / (run_id + "." + kind)
        with target.open("xb") as stream:
            stream.write(content)
        receipt = {
            "run_id": run_id,
            "received_at": datetime.now(UTC).isoformat(),
            "path": str(target),
            "bytes": len(content),
            "capture_source": "actual_cua_browser_screenshot",
            "annotation": "Raw browser PNG/JPEG transfer only; no synthesized screenshot or redraw.",
        }
        with (
            guard,
            (args.output / "browser-screenshots.jsonl").open(
                "a", encoding="utf-8"
            ) as stream,
        ):
            stream.write(json.dumps(receipt) + "\n")
        return receipt

    service = gr.mount_gradio_app(service, panel, path="/", js=js)
    (args.output / "service.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": args.port,
                "workload": args.workload,
                "temperature": args.temperature,
                "repeat": args.repeat,
                "started_at": datetime.now(UTC).isoformat(),
                "fixtures": str(args.fixtures),
                "source_kind": "source_backed_replay_for_ui_timing",
                "cloud_calls": 0,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    uvicorn.run(
        service, host="127.0.0.1", port=args.port, log_level="warning", access_log=False
    )


if __name__ == "__main__":
    main()
