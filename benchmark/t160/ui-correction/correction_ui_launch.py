"""Launch committed correction UI with explicit repository, batch, and snapshot paths."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--workload", choices=["text", "figure", "cross"])
    parser.add_argument("--temperature", choices=["cold", "warm"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--port", type=int, default=7871)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-replays", action="store_true")
    parser.add_argument("--quality-pointer", type=Path)
    parser.add_argument("--quality-output", type=Path)
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    run = args.batch_root.resolve()
    source = args.source_root.resolve()
    scripts = Path(__file__).resolve().parent
    isolation = json.loads((run / "isolation.json").read_text(encoding="utf-8"))
    assert isolation["database"].startswith("eduagent_e2_acceptance_")
    if not (source / "backend/app/ui/paper_correction_view.py").is_file():
        raise ValueError(
            "Source snapshot does not contain the actual correction panel."
        )
    env = os.environ.copy()
    env.update({k: v for k, v in dotenv_values(repo / ".env").items() if v is not None})
    os.environ.update(env)
    sys.path.insert(0, str(source))
    from backend.app.core.config import get_settings

    settings = get_settings()
    env.update(
        DATABASE_URL=make_url(str(settings.database_url))
        .set(database=isolation["database"])
        .render_as_string(hide_password=False),
        REDIS_URL=f"redis://127.0.0.1:{isolation['redis_port']}/0",
        STORAGE_ROOT=str(run / "business-files"),
        PYTHONPATH=str(source),
        PYTHONIOENCODING="utf-8",
        VISION_MODEL="",
    )
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.prepare_replays:
        if args.quality_pointer is None:
            parser.error(
                "--quality-pointer required when preparing source-backed replays."
            )
        cmd = [
            sys.executable,
            str(scripts / "correction_ui_replay.py"),
            "--quality-pointer",
            str(args.quality_pointer.resolve()),
            "--output",
            str(args.fixtures.resolve()),
        ]
        if args.quality_output is not None:
            cmd.extend(["--quality-output", str(args.quality_output.resolve())])
        log_name = "replay-prepare.log"
    else:
        if args.workload is None or args.temperature is None:
            parser.error(
                "--workload and --temperature required for an actual UI worker."
            )
        cmd = [
            sys.executable,
            str(scripts / "correction_ui_server.py"),
            "--fixtures",
            str(args.fixtures.resolve()),
            "--workload",
            args.workload,
            "--temperature",
            args.temperature,
            "--repeat",
            str(args.repeat),
            "--port",
            str(args.port),
            "--output",
            str(out),
        ]
        log_name = "server.log"
    with (out / log_name).open("xb") as log:
        result = subprocess.run(
            cmd, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
        )
    print(
        json.dumps({"exit_code": result.returncode, "log": str(out / log_name)}),
        flush=True,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
