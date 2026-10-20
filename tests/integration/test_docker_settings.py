"""Actual T235 Compose lifecycle; requires an explicitly selected local test image.

Without the opt-in image/root this test fails closed before invoking Docker.
The caller must run this external-resource test separately from pure unit tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


class DockerRun:
    def __init__(self, root: Path, image: str):
        self.root = root
        self.project = "eduagent-t235-" + uuid4().hex[:12]
        self.environment_id = str(uuid4())
        self.ids = {}
        self.token = None
        self.client = None
        self.port = None
        self.image = self.docker("image", "inspect", image, "--format", "{{.Id}}")
        self.env = {k: v for k, v in os.environ.items() if k.upper() in {
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "USERPROFILE",
            "HOME", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES",
        }}
        self.env["COMPOSE_DISABLE_ENV_FILE"] = "1"
        root.mkdir(parents=True, exist_ok=False)
        self.secrets = root / "secrets"
        self.secrets.mkdir()
        self.empty = root / "empty-secrets"
        self.empty.mkdir()
        self.profile_dir = root / "profile"
        self.profile_dir.mkdir()
        self.password = "synthetic-t235-db-" + uuid4().hex
        self.jwt = "synthetic-t235-jwt-" + uuid4().hex
        self.jwt2 = "synthetic-t235-jwt2-" + uuid4().hex
        for name, value in {"database-v1": self.password, "jwt-v1": self.jwt, "jwt-v2": self.jwt2}.items():
            (self.secrets / name).write_text(value, encoding="utf-8")
        self.hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.secrets.iterdir()}
        self.env_file = root / "compose.env"
        self.env_file.write_text("POSTGRES_USER=eduagent_test_admin\nPOSTGRES_DB=postgres\nPOSTGRES_PORT=0\nREDIS_PORT=0\nBACKEND_PORT=0\nEDUAGENT_ENVIRONMENT=test\n", encoding="utf-8")
        self.overlay = root / "compose.test.json"
        self.write_overlay(self.secrets)

    def docker(self, *args, input=None):
        result = subprocess.run(["docker", *args], input=input, capture_output=True,
                                text=True, encoding="utf-8", check=False)
        if result.returncode:
            raise RuntimeError("Docker operation failed: " + " ".join(args[:2]))
        return result.stdout.strip()

    def compose(self, *args, input=None):
        command = ["docker", "compose", "--project-name", self.project, "--env-file", str(self.env_file),
                   "--project-directory", str(ROOT), "--file", str(ROOT / "docker-compose.yml"),
                   "--file", str(self.overlay), *args]
        result = subprocess.run(command, input=input, env=self.env, cwd=ROOT,
                                text=True, encoding="utf-8", capture_output=True, check=False)
        if result.returncode:
            # No rendered Compose environment or secret values in pytest output.
            raise RuntimeError("Compose operation failed: " + " ".join(args[:2]))
        return result.stdout.strip()

    def write_overlay(self, directory):
        catalog = {name: {"path": "/run/secrets/" + name, "purpose": purpose} for name, purpose in
                   (("database-v1", "database"), ("jwt-v1", "jwt"), ("jwt-v2", "jwt"), ("missing-provider", "provider"))}
        model = {"services": {
            "postgres": {"restart": "no"}, "redis": {"restart": "no"},
            "backend": {"build": None, "image": self.image, "restart": "no",
                "environment": {"DATABASE_URL": None, "REDIS_URL": None,
                    "EDUAGENT_TEST_RESOURCE_PROFILE": "/run/eduagent/profile/test.json",
                    "EDUAGENT_SECRET_MOUNTS": json.dumps(catalog)},
                "volumes": [
                    {"type": "bind", "source": str(self.profile_dir), "target": "/run/eduagent/profile", "read_only": True,
                     "bind": {"create_host_path": False}},
                    {"type": "bind", "source": str(directory), "target": "/run/secrets", "read_only": True,
                     "bind": {"create_host_path": False}},
                ]}}}
        self.overlay.write_text(json.dumps(model), encoding="utf-8")

    def inspect(self, service):
        identifier = self.compose("ps", "--all", "--quiet", service)
        assert identifier and "\n" not in identifier
        obj = json.loads(self.docker("inspect", identifier))[0]
        labels = obj["Config"]["Labels"]
        assert labels["com.docker.compose.project"] == self.project
        assert labels["com.docker.compose.service"] == service
        self.ids[service] = identifier
        (self.root / "resources.json").write_text(json.dumps({"project": self.project, "containers": self.ids}), encoding="utf-8")
        return obj

    def sql(self, query, *, database="postgres", role="eduagent_test_admin"):
        return self.compose("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", role,
                            "-d", database, "-At", input=query)

    def start(self):
        assert not self.docker("ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + self.project)
        assert not self.docker("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + self.project)
        self.compose("up", "-d", "--no-build", "--pull", "never", "--wait", "postgres", "redis")
        for service in ("postgres", "redis"):
            obj = self.inspect(service)
            assert obj["State"]["Health"]["Status"] == "healthy"
            assert all(row["HostIp"] == "127.0.0.1" for rows in obj["NetworkSettings"]["Ports"].values() for row in rows)
        instance = self.sql("SELECT system_identifier::text FROM pg_control_system();").strip()
        assert instance.isdigit()
        self.sql("CREATE ROLE eduagent_test_runner LOGIN PASSWORD '" + self.password + "';\n"
                 "CREATE DATABASE eduagent_test OWNER eduagent_test_runner;")
        self.sql("CREATE EXTENSION vector; GRANT EXECUTE ON FUNCTION pg_control_system() TO eduagent_test_runner;",
                 database="eduagent_test")
        actual = self.sql("SELECT current_database(), current_user, current_schema(), system_identifier::text FROM pg_control_system();",
                          database="eduagent_test", role="eduagent_test_runner")
        assert actual == "eduagent_test|eduagent_test_runner|public|" + instance
        redis_info = self.compose("exec", "-T", "redis", "redis-cli", "INFO", "server")
        redis_id = next(line.split(":", 1)[1] for line in redis_info.splitlines() if line.startswith("run_id:"))
        profile = {"environment": "test", "environment_id": self.environment_id, "root": "/app/storage",
            "database_identity": {"host": "postgres", "port": 5432, "database": "eduagent_test",
                "user": "eduagent_test_runner", "schema_name": "public", "instance_id": instance},
            "redis_instance_id": redis_id}
        (self.profile_dir / "test.json").write_text(json.dumps(profile), encoding="utf-8")
        self.start_backend()

    def start_backend(self, *, recreate=False):
        if self.client:
            self.client.close()
        args = ["up", "-d", "--no-build", "--pull", "never"]
        if recreate:
            args.append("--force-recreate")
        self.compose(*args, "backend")
        obj = self.inspect("backend")
        assert obj["Image"] == self.image
        bindings = obj["NetworkSettings"]["Ports"]["8000/tcp"]
        assert len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1"
        self.port = int(bindings[0]["HostPort"])
        self.url = f"http://127.0.0.1:{self.port}"
        self.client = httpx.Client(base_url=self.url, timeout=15, trust_env=False)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                response = self.client.get("/setup-ready")
                if response.status_code == 200:
                    assert response.json()["setup"]["static_resources"] == "available"
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.25)
        raise AssertionError("Actual container Setup did not become ready")

    def authorize(self):
        url = self.compose("exec", "-T", "backend", "python", "-m", "backend.app.deployment.container_control", "--port", str(self.port))
        parts = urlsplit(url)
        assert parts.netloc == f"127.0.0.1:{self.port}" and not parts.query
        code = parse_qs(parts.fragment)["code"][0]
        response = self.client.post("/api/settings/session/exchange", json={"one_time_code": code}, headers={"Origin": self.url})
        assert response.status_code == 200
        self.token = response.json()["management_token"]
        repeated = self.client.post("/api/settings/session/exchange", json={"one_time_code": code}, headers={"Origin": self.url})
        assert repeated.status_code in (401, 403)

    def api(self, method, path, body=None, expected=(200,)):
        response = self.client.request(method, "/api/settings" + path, json=body,
                                      headers={"Authorization": "Setup " + self.token, "Origin": self.url})
        assert response.status_code in expected, (path, response.status_code, response.json().get("code"))
        return response.json() if response.content else None

    def stop_backend(self):
        self.compose("stop", "--timeout", "15", "backend")
        obj = self.inspect("backend")
        assert not obj["State"]["Running"] and obj["State"]["ExitCode"] == 0

    def close(self):
        if self.client:
            self.client.close()
        # Every destructive command is scoped to this newly created project.
        actual = self.docker("ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + self.project).splitlines()
        for identifier in actual:
            obj = json.loads(self.docker("inspect", identifier))[0]
            service = obj["Config"]["Labels"]["com.docker.compose.service"]
            assert service in {"postgres", "redis", "backend"}
            assert obj["Config"]["Labels"]["com.docker.compose.project"] == self.project
            if service in self.ids:
                assert identifier == self.ids[service]
        for kind in ("volume", "network"):
            names = self.docker(kind, "ls", "-q", "--filter", "label=com.docker.compose.project=" + self.project).splitlines()
            for name in names:
                obj = json.loads(self.docker(kind, "inspect", name))[0]
                assert obj["Labels"]["com.docker.compose.project"] == self.project
        self.compose("down", "--volumes", "--timeout", "15")
        assert not self.docker("ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + self.project)
        assert not self.docker("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + self.project)
        assert not self.docker("network", "ls", "-q", "--filter", "label=com.docker.compose.project=" + self.project)


@pytest.fixture
def docker_run():
    image = os.environ.get("EDUAGENT_DOCKER_SETTINGS_IMAGE")
    root = os.environ.get("EDUAGENT_DOCKER_SETTINGS_ROOT")
    if not image or not root:
        pytest.fail("not_run: requires explicit EDUAGENT_DOCKER_SETTINGS_IMAGE and owned ROOT")
    selected = Path(root).resolve()
    assert selected.is_relative_to(ROOT / ".cache")
    run = DockerRun(selected / uuid4().hex, image)
    try:
        run.start()
        yield run
    finally:
        run.close()


def test_actual_docker_settings_lifecycle(docker_run):
    run = docker_run
    assert run.client.get("/settings").status_code == 200
    assert run.client.get("/ready").status_code == 503
    assert run.client.get("/api/settings/").status_code in (401, 403)
    run.authorize()
    mount = next(m for m in run.inspect("backend")["Mounts"] if m["Destination"] == "/run/secrets")
    assert mount["RW"] is False
    assert run.compose("exec", "-T", "backend", "python", "-c",
        "import pathlib,stat; p=pathlib.Path('/app/storage/temp/local-management/setup.sock'); print(oct(stat.S_IMODE(p.stat().st_mode)))") == "0o600"
    assert run.client.post("/api/settings/drafts", json={"changes": {}}, headers={
        "Authorization": "Setup " + run.token, "Origin": "https://foreign.invalid"}).status_code == 403
    run.api("POST", "/credentials", {"purpose": "jwt", "secret_value": "synthetic-denied"}, (405,))
    bridge_code = "import urllib.request,urllib.error;\ntry: urllib.request.urlopen('http://backend:8000/api/settings/')\nexcept urllib.error.HTTPError as e: print(e.code)"
    assert run.docker("run", "--rm", "--pull", "never", "--network", run.project + "_default",
                      "--label", "eduagent.test.run=" + run.project, run.image, "python", "-c", bridge_code) in {"401", "403"}
    catalog = run.api("GET", "/")["credential_statuses"]
    def ref(name):
        row = next(item for item in catalog if item["external_secret_id"] == name)
        return {"credential_id": row["credential_ref"], **{key: row[key] for key in
                ("version", "environment_id", "backend", "purpose", "external_secret_id")}}
    # Explicit provisioning of a new, verified Test volume, never replacement user storage.
    run.compose("exec", "-T", "backend", "python", "-c", "from pathlib import Path; Path('/app/storage/storage').mkdir(exist_ok=False)")
    schema = "test_" + uuid4().hex
    run.sql('CREATE SCHEMA "' + schema + '";', database="eduagent_test", role="eduagent_test_runner")
    assert run.sql("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='" + schema + "';",
                   database="eduagent_test") == "eduagent_test_runner"
    business = {"database_locator": {"host": "postgres", "port": 5432, "database": "eduagent_test", "user": "eduagent_test_runner", "schema": schema},
        "database_password_ref": ref("database-v1"), "redis_locator": {"host": "redis", "port": 6379},
        "jwt_secret_ref": ref("jwt-v1"), "storage_root": "/app/storage/storage"}
    draft = run.api("POST", "/drafts", {"base_revision": None, "changes": {"business": business}, "acknowledge_override": True}, (201,))["revision_id"]
    assert run.api("POST", f"/drafts/{draft}/validate", {"expected_revision": None})["valid"]
    preview = run.api("POST", "/initialization/preview", {"draft_revision": draft})
    assert preview["allowed"]
    operation = run.api("POST", "/initialization/apply", {"confirmation_id": preview["preview_id"], "expected_revision": draft}, (202,))
    for _ in range(360):
        operation = run.api("GET", "/operations/" + operation["operation_id"])
        if operation["status"] in {"succeeded", "failed", "interrupted"}:
            break
        time.sleep(.25)
    assert operation["status"] == "succeeded", operation.get("error_code")
    active = run.api("POST", f"/drafts/{draft}/activate", {"expected_active_revision": None, "probe_ids": [], "acknowledge_override": True}, (200, 202))["revision_id"]
    assert run.client.get("/ready").status_code == 503
    run.stop_backend()
    run.start_backend()
    run.authorize()
    assert run.client.get("/ready").status_code == 200
    assert run.client.get("/gradio/").status_code == 200
    assert run.api("GET", "/")["active_revision"] == active
    # Missing DB preserves Setup, then recovers without altering configuration.
    run.compose("stop", "--timeout", "15", "postgres")
    assert run.client.get("/setup-ready").status_code == 200
    assert run.client.get("/ready").status_code == 503
    run.compose("up", "-d", "--no-build", "--pull", "never", "--wait", "postgres")
    assert run.client.get("/ready").status_code == 200
    # Remove only the mount, retaining the exact host secret files and persistent config.
    run.stop_backend()
    run.write_overlay(run.empty)
    run.start_backend(recreate=True)
    run.authorize()
    assert run.client.get("/setup-ready").status_code == 200
    assert run.client.get("/ready").status_code == 503
    statuses = run.api("GET", "/")["credential_statuses"]
    assert any(item["status"] in {"unavailable", "absent"} for item in statuses)
    run.stop_backend()
    run.write_overlay(run.secrets)
    run.start_backend(recreate=True)
    run.authorize()
    assert run.client.get("/ready").status_code == 200
    # Rotation binds a separately mounted immutable ID; old bytes stay unchanged.
    business["jwt_secret_ref"] = ref("jwt-v2")
    draft = run.api("POST", "/drafts", {"base_revision": active, "changes": {"business": business}, "acknowledge_override": True}, (201,))["revision_id"]
    assert run.api("POST", f"/drafts/{draft}/validate", {"expected_revision": active})["valid"]
    active = run.api("POST", f"/drafts/{draft}/activate", {"expected_active_revision": active, "probe_ids": [], "acknowledge_override": True}, (200, 202))["revision_id"]
    run.stop_backend()
    run.start_backend()
    run.authorize()
    assert run.client.get("/ready").status_code == 200
    assert run.api("GET", "/")["active_revision"] == active
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.secrets.iterdir()} == run.hashes
    logs = run.compose("logs", "--no-color", "backend")
    assert all(value not in logs for value in (run.password, run.jwt, run.jwt2, run.token))
    run.stop_backend()
