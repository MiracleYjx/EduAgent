"""Real frozen Windows checks with explicit Test identity and synthetic secrets.

No dotenv is read or written. Results go to stdout; runtime logs and resource
samples are ordinary machine artifacts in the caller's new Test run directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
from sqlalchemy.engine import make_url

from backend.app.core.config import AppSettings, install_runtime_settings
from backend.app.core.environment import load_test_resource_profile
from backend.app.deployment.control_channel import ControlChannelError, ControlClient
from benchmark.t160.resource_sampler import windows_process_parents
from tests.postgres_helpers import isolated_postgres_schema

OS_ENV = frozenset({"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
                    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PROGRAMFILES",
                    "PROGRAMFILES(X86)", "PROGRAMDATA", "OS"})


def emit(**facts):
    print(json.dumps(facts, ensure_ascii=False), flush=True)


def child_environment(profile_path: Path, run: Path, exe: Path):
    # Direct worker probes supply the same owned directories prepared by Launcher.
    data = Path(json.loads(profile_path.read_text(encoding="utf-8"))["root"])
    if not data.is_relative_to(run):
        raise ValueError("TEST_WORKER_ROOT_MISMATCH")
    for name in ("storage", "cache", "logs", "temp", "results"):
        (data / name).mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k.upper() in OS_ENV}
    env["PATH"] = str(Path(env["SYSTEMROOT"]) / "System32")
    for key in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
        path = run / key
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    env.update(EDUAGENT_ENVIRONMENT="test", EDUAGENT_RUNTIME_KIND="windows",
               EDUAGENT_TEST_RESOURCE_PROFILE=str(profile_path),
               EDUAGENT_SETTINGS_UI_ROOT=str(exe.parent / "_internal/settings-ui"),
               GRADIO_ANALYTICS_ENABLED="false", HF_HUB_OFFLINE="1", DO_NOT_TRACK="1")
    return env


class FrozenWorker:
    """Use the real package's owned anonymous control channel; never log grants."""
    def __init__(self, exe, profile_path, run):
        self.exe, self.profile_path, self.run = exe, profile_path, run
        self.process = self.control = self.http = self.log = None
        self.token = None
        self.port = None

    def start(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        env = child_environment(self.profile_path, self.run, self.exe)
        env.update(EDUAGENT_BIND_HOST="127.0.0.1", EDUAGENT_BIND_PORT=str(self.port))
        self.log = (self.run / "worker.log").open("a", encoding="utf-8")
        started = time.perf_counter()
        self.process = subprocess.Popen(
            [str(self.exe), "--child", "serve", "--port", str(self.port),
             "--bind-host", "127.0.0.1"], cwd=self.run, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.control = ControlClient(reader=self.process.stdout, writer=self.process.stdin,
                                     settings_base_url=self.url)
        registration = self.control.wait_registration(timeout=90, settings_base_url=self.url)
        assert registration["pid"] == self.process.pid
        self.http = httpx.Client(base_url=self.url, timeout=15, trust_env=False)
        ready = self.http.get("/setup-ready")
        assert ready.status_code == 200 and ready.json()["setup"]["static_resources"] == "available"
        self.setup_seconds = time.perf_counter() - started
        grant = self.control.open_settings(timeout=5)
        exchanged = self.http.post("/api/settings/session/exchange",
            json={"one_time_code": grant["code"]}, headers={"Origin": self.url})
        assert exchanged.status_code == 200
        self.token = exchanged.json()["management_token"]
        return self

    def request(self, method, path, body=None, expected=(200,)):
        headers = {"Authorization": f"Setup {self.token}", "Origin": self.url}
        response = self.http.request(method, "/api/settings" + path, json=body, headers=headers)
        if response.status_code not in expected:
            # Retain safe API classification, never raw response/provider material.
            code = response.json().get("code", "UNEXPECTED_STATUS")
            raise RuntimeError(f"{method} {path}: {response.status_code} {code}")
        return response.json() if response.content else None

    def stop(self):
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None and self.control is not None:
                try:
                    self.control.shutdown(timeout=5)
                    process.wait(timeout=10)
                except (ControlChannelError, OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=10)
            assert process.returncode == 0, f"Frozen worker exit: {process.returncode}"
            assert process.pid not in windows_process_parents()
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", self.port))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            if self.control is not None:
                self.control.close()
            for stream in (process.stdin, process.stdout, self.log):
                if stream is not None:
                    stream.close()
            if self.http is not None:
                self.http.close()
            self.process = None


@contextmanager
def running(exe, profile_path, run):
    worker = FrozenWorker(exe, profile_path, run)
    try:
        yield worker.start()
    finally:
        worker.stop()


def reference(body):
    return {"credential_id": body["credential_ref"],
            **{key: body[key] for key in ("version", "environment_id", "backend", "purpose")}}


def credential(worker, purpose, value):
    body = worker.request("POST", "/credentials", {"purpose": purpose, "secret_value": value}, (201,))
    assert body["status"] == "available" and body["backend"] == "dpapi"
    assert value not in json.dumps(body)
    return reference(body)


def draft_and_validate(worker, changes, base):
    draft = worker.request("POST", "/drafts", {"base_revision": base, "changes": changes}, (201,))
    revision = draft["revision_id"]
    result = worker.request("POST", f"/drafts/{revision}/validate", {"expected_revision": base})
    assert result["valid"] is True, "Draft failed validation"
    return revision


def activate(worker, revision, active):
    return worker.request("POST", f"/drafts/{revision}/activate",
        {"expected_active_revision": active, "probe_ids": [], "acknowledge_override": True}, (200, 202))


def initialize(worker, revision):
    preview = worker.request("POST", "/initialization/preview", {"draft_revision": revision})
    assert preview["allowed"] is True
    operation = worker.request("POST", "/initialization/apply",
        {"confirmation_id": preview["preview_id"], "expected_revision": revision}, (202,))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = worker.request("GET", "/operations/" + operation["operation_id"])
        if state["status"] in {"succeeded", "failed", "interrupted"}:
            assert state["status"] == "succeeded", "Initialization: " + str(state.get("error_code"))
            return
        time.sleep(.1)
    raise RuntimeError("Initialization timed out")


def require_business(worker):
    ready = worker.http.get("/ready")
    if ready.status_code != 200:
        emit(failed_ready=ready.json(), prerequisite_checks=worker.request("GET", "/readiness")["business_prerequisites"]["checks"])
    assert ready.status_code == 200, "Frozen business readiness failed"
    assert worker.http.get("/gradio/").status_code == 200, "Frozen workbench failed"
    view = worker.request("GET", "/")
    assert all(row["status"] == "available" for row in view["credential_statuses"])
    return view


def check_secret_files(run, values):
    for path in run.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".log", ".dpapi", ".env"}:
            payload = path.read_bytes()
            assert all(value.encode() not in payload for value in values), "Synthetic secret leaked"
    assert not list(run.rglob("*.env"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--test-profile", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--credential-only", action="store_true", help="Focused fault recovery; not the 3/5 timing protocol")
    args = parser.parse_args()
    exe, profile_path, run_root = (p.resolve() for p in (args.exe, args.test_profile, args.run_root))
    if os.name != "nt" or not exe.is_file() or not (exe.parent / "build-receipt.json").is_file():
        raise ValueError("A completed real Windows package is required")
    if not run_root.is_relative_to(ROOT / ".cache"):
        raise ValueError("Use a new owned repository Test run directory")
    os.environ.update(EDUAGENT_ENVIRONMENT="test", EDUAGENT_TEST_RESOURCE_PROFILE=str(profile_path))
    profile = load_test_resource_profile()
    if not run_root.is_relative_to(profile.root):
        raise ValueError("Run directory must belong to the explicit Test profile")
    db = make_url(os.environ["EDUAGENT_TEST_DATABASE_URL"])
    redis = make_url(os.environ["EDUAGENT_TEST_REDIS_URL"])
    identity = profile.database_identity
    if (db.host, db.port, db.database, db.username) != (identity.host, identity.port, identity.database, identity.user):
        raise ValueError("TEST_DATABASE_URL_MISMATCH")
    # URLs are held in memory; AppSettings explicitly disables dotenv.
    settings = AppSettings(_env_file=None, database_url=db.render_as_string(hide_password=False),
        redis_url=redis.render_as_string(hide_password=False), storage_root=profile.root / "storage",
        confidence_threshold=.8, JWT_SECRET_KEY="synthetic-t234-observer-not-used-by-package",
        llm_provider="none", embedding_provider="none", rerank_provider="none")
    install_runtime_settings(settings)
    from redis import Redis
    redis_client = Redis.from_url(redis.render_as_string(hide_password=False), socket_timeout=3)
    try:
        if redis_client.info("server").get("run_id") != profile.redis_instance_id:
            raise ValueError("TEST_REDIS_IDENTITY_MISMATCH")
    finally:
        redis_client.close()
    run_root.mkdir(parents=True, exist_ok=False)
    secrets = ["synthetic-t234-jwt-" + uuid4().hex, "synthetic-t234-reentry-" + uuid4().hex]
    if db.password:
        secrets.append(db.password)
    emit(protocol="v3-settings-derived-from-v2", exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
         first_repetitions=1 if args.credential_only else 3, subsequent_repetitions=0 if args.credential_only else 5, paid_model_calls=0,
         original_browser_timing="not_run: headless worker HTTP/control observations",
         other_user_dpapi="not_run: no independently authorized Windows test account")
    first_repetitions = 1 if args.credential_only else 3
    subsequent_repetitions = 0 if args.credential_only else 5
    rows = []
    with ExitStack() as stack:
        first = None
        for i in range(first_repetitions):
            _engine, schema = stack.enter_context(isolated_postgres_schema())
            run = run_root / f"first-{i+1}"
            run.mkdir()
            derived = profile.model_copy(update={"root": run / "data", "environment_id": uuid4()})
            child_profile = run / "profile.json"
            child_profile.write_text(derived.model_dump_json(), encoding="utf-8")
            started = time.perf_counter()
            with running(exe, child_profile, run) as worker:
                assert worker.http.get("/ready").status_code == 503
                setup = worker.setup_seconds
                jwt = credential(worker, "jwt", secrets[0])
                password = credential(worker, "database", db.password) if db.password else None
                business = {"database_locator": {"host": db.host, "port": db.port,
                    "database": db.database, "user": db.username, "schema": schema},
                    "database_password_ref": password,
                    "redis_locator": {"host": redis.host, "port": redis.port or 6379, "database": 0},
                    "storage_root": str(derived.root / "storage"), "jwt_secret_ref": jwt}
                revision = draft_and_validate(worker, {"business": business}, None)
                initialize(worker, revision)
                revision = activate(worker, revision, None)["revision_id"]
            with running(exe, child_profile, run) as worker:
                require_business(worker)
                elapsed = time.perf_counter() - started
            row = {"mode": "first", "repeat": i+1, "setup_seconds": setup,
                   "business_flow_seconds": elapsed, "original_business_target_seconds": 30,
                   "headless_flow_below_target": elapsed < 30, "exit_code": 0}
            rows.append(row)
            emit(**row)
            if first is None:
                first = (run, child_profile, jwt, revision, business)
        run, child_profile, jwt, revision, business = first
        for i in range(subsequent_repetitions):
            started = time.perf_counter()
            with running(exe, child_profile, run) as worker:
                require_business(worker)
                elapsed = time.perf_counter() - started
                setup = worker.setup_seconds
            row = {"mode": "subsequent", "repeat": i+1, "setup_seconds": setup,
                   "business_flow_seconds": elapsed, "original_business_target_seconds": 10,
                   "headless_flow_below_target": elapsed < 10, "exit_code": 0}
            rows.append(row)
            emit(**row)
        blob = run / "data/credentials" / f"{jwt['credential_id']}.jwt.{jwt['version']}.dpapi"
        assert blob.is_file()
        original = blob.read_bytes()
        blob.write_bytes(b"corrupt-synthetic-ciphertext")
        with running(exe, child_profile, run) as worker:
            assert worker.http.get("/ready").status_code == 503
            states = worker.request("GET", "/")["credential_statuses"]
            assert any(row["status"] == "reentry_required" for row in states)
        blob.write_bytes(original)
        with running(exe, child_profile, run) as worker:
            require_business(worker)
        blob.unlink()
        with running(exe, child_profile, run) as worker:
            assert worker.http.get("/ready").status_code == 503
            replacement = worker.request("POST", f"/credentials/{jwt['credential_id']}/versions",
                                        {"secret_value": secrets[1]}, (201,))
            business = {**business, "jwt_secret_ref": reference(replacement)}
            next_revision = draft_and_validate(worker, {"business": business}, revision)
            activate(worker, next_revision, revision)
        with running(exe, child_profile, run) as worker:
            require_business(worker)
        check_secret_files(run_root, secrets)
        emit(same_user_dpapi="passed", corrupted_ciphertext="passed", missing_key_reentry="passed",
             restart_persistence="passed", secret_scan="passed", owned_process_cleanup="passed")
    emit(owned_schema_cleanup="passed", first_runs=first_repetitions, subsequent_runs=subsequent_repetitions,
         original_browser_timing="not_run", other_user_dpapi="not_run", result="PARTIAL")
    return 2  # Required cross-user and original browser protocol evidence is still absent.


if __name__ == "__main__":
    raise SystemExit(main())
