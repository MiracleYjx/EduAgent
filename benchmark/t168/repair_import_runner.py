"""Remeasure the frozen 15 import cases in an owned database, retaining failures."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ocr-model-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "benchmark/results"):
        raise ValueError("Use a fresh repository result directory")
    output.mkdir(parents=True, exist_ok=False)
    original = make_url(str(get_settings().database_url))
    name = "eduagent_e2_acceptance_" + uuid4().hex[:12]
    admin = create_engine(
        original.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    created = False
    exit_code = None
    removed = False
    active = None
    previous = os.environ.get("DATABASE_URL")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        (output / "isolation.json").write_text(
            json.dumps(
                {
                    "database": name,
                    "redis_port": 6379,
                    "pg_container": "eduagent-postgres-1",
                    "redis_container": "eduagent-redis-1",
                    "original_database_untouched": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.environ["DATABASE_URL"] = original.set(database=name).render_as_string(
            hide_password=False
        )
        get_settings.cache_clear()
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        arguments = [
            sys.executable,
            str(ROOT / "benchmark/t160/quality_runner.py"),
            "--execute-confirmed",
            "--run-root",
            str(output),
            "--repo-root",
            str(ROOT),
            "--source-root",
            str(ROOT),
            "--ocr-model-dir",
            str(args.ocr_model_dir.resolve()),
        ]
        # No credentials in arguments, stdout or the public receipt.
        with (output / "execution.log").open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                arguments,
                cwd=ROOT,
                stdout=stream,
                stderr=stream,
                env=os.environ | {"PYTHONIOENCODING": "utf-8"},
                check=False,
            )
        exit_code = result.returncode
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        if created:
            with admin.connect() as conn:
                active = conn.scalar(
                    text("SELECT count(*) FROM pg_stat_activity WHERE datname=:name"),
                    {"name": name},
                )
                if active == 0 and name != original.database:
                    conn.execute(text(f'DROP DATABASE "{name}"'))
                    removed = True
        admin.dispose()
        (output / "cleanup.json").write_text(
            json.dumps(
                {
                    "database": name,
                    "active_connections": active,
                    "database_removed": removed,
                    "runner_exit_code": exit_code,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "baseline": "AI-assisted + developer review; independent teachers=0",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    raise SystemExit(exit_code if exit_code is not None else 1)


if __name__ == "__main__":
    main()
