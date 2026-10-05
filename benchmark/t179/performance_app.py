"""Mount the real ExamView for browser measurements; login/setup is outside timing."""

from __future__ import annotations

import argparse
import cProfile
import functools
import json
import os
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import gradio as gr
import uvicorn

from backend.app.core.app import create_app
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import UserRole
from backend.app.models import User
from backend.app.services.auth_service import AuthService
from backend.app.ui.exam_view import create_exam_view
from benchmark.t179.fixtures import require_isolation


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    settings = require_isolation()
    spec = json.loads(args.fixtures.read_text(encoding="utf-8"))
    with get_session_factory()() as session:
        actor = session.get(User, UUID(spec["teacher_id"]))
        assert actor and actor.is_active
        auth = AuthService(session)
        token = auth.issue_access_token(actor)
        actual = auth.get_current_user(token)
        assert any(role.name == UserRole.TEACHER for role in actual.roles)
        state = {
            "access_token": token,
            "user_id": str(actual.id),
            "roles": ["Teacher"],
            "username": actual.username,
        }
    with gr.Blocks(title="T179 real ExamView acceptance") as demo:
        view = create_exam_view(gr.State(state))
        view.panel.visible = True
        view.exams_table.elem_id = "t179-exams"
        demo.load(lambda: gr.update(visible=True), outputs=view.panel)
    if os.environ.get("T179_PROFILE") == "1":
        for registration in demo.fns.values():
            original = registration.fn
            if original and original.__name__ == "assemble":

                @functools.wraps(original)
                def measured(*values, _original=original, **kwargs):
                    profile = cProfile.Profile()
                    profile.enable()
                    try:
                        return _original(*values, **kwargs)
                    finally:
                        profile.disable()
                        profile.dump_stats(
                            str(args.output.parent / "assemble-profile.pstats")
                        )

                registration.fn = measured
    demo.queue()
    app = create_app(settings=settings, gradio_app=demo)

    @app.get("/acceptance/worker")
    def worker():
        return {
            "pid": os.getpid(),
            "source": "real uvicorn worker; no subprocess inference",
            "view": "backend.app.ui.exam_view.create_exam_view",
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": args.port,
                "view": "real ExamView; actual services; prefilled real synthetic teacher JWT; login outside timing",
            }
        ),
        encoding="utf-8",
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
