"""TCR-S3-READINESS-021（T219）：分层就绪与核心依赖诊断单元契约。

只用假探针与假引擎/客户端：不连真实 PG/Redis、不调用模型、不读真实凭据。本目录
`conftest.py` 阻断真实 socket，因此任何“靠真实连接完成评估”的实现都会在此暴露。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, cast

import pytest
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import AppSettings
from backend.app.schemas.settings import DiagnosticsReport, ReadinessReport
from backend.app.services.settings import readiness
from backend.app.services.settings.readiness import (
    DatabaseProbeResult,
    DependencyProbes,
    ProbeOutcome,
    ReadinessInputs,
    discover_migration_head,
    evaluate_readiness,
    gate_ready,
    probe_database,
    probe_redis,
    probe_storage,
    run_diagnostics,
)
from tests.unit.settings_helpers import build_test_settings

OK = ProbeOutcome("ok")
FAILED_POSTGRES = ProbeOutcome(
    "failed", code="postgres_unavailable", detail="PostgreSQL 未就绪。"
)
FAILED_REDIS = ProbeOutcome("failed", code="redis_unavailable", detail="Redis 未就绪。")
FAILED_STORAGE = ProbeOutcome("failed", code="storage_unavailable", detail="存储目录不可用。")
FAILED_VECTOR = ProbeOutcome("failed", code="vector_missing", detail="pgvector 扩展未安装。")
FAILED_MIGRATION = ProbeOutcome(
    "failed", code="migration_outdated", detail="数据库迁移版本与代码不一致。"
)
FAILED_SCHEMA = ProbeOutcome("failed", code="schema_drift", detail="缺少 1 张必需业务表。")
UNKNOWN_MIGRATION = ProbeOutcome(
    "unknown", code="migration_head_unavailable", detail="未判断版本一致性。"
)


def _probes(
    *,
    postgres: ProbeOutcome = OK,
    vector: ProbeOutcome = OK,
    migration: ProbeOutcome = OK,
    schema: ProbeOutcome = OK,
    redis: ProbeOutcome = OK,
    storage: ProbeOutcome = OK,
    calls: list[str] | None = None,
) -> DependencyProbes:
    def database(_settings: AppSettings, _head: str | None) -> DatabaseProbeResult:
        if calls is not None:
            calls.append("database")
        return DatabaseProbeResult(
            postgres=postgres, vector=vector, migration=migration, schema=schema
        )

    def redis_probe(_settings: AppSettings) -> ProbeOutcome:
        if calls is not None:
            calls.append("redis")
        return redis

    def storage_probe(_settings: AppSettings) -> ProbeOutcome:
        if calls is not None:
            calls.append("storage")
        return storage

    return DependencyProbes(
        database=database, redis=redis_probe, storage=storage_probe
    )


def _inputs(
    *,
    configured: bool = True,
    settings: AppSettings | None = None,
    assembly: str = "assembled",
    recovery: str = "completed",
    credential_backend_available: bool | None = None,
    setup_ready: bool = True,
    static_resources: str = "available",
) -> ReadinessInputs:
    business = settings
    if business is None and configured:
        business = build_test_settings()
    return ReadinessInputs(
        environment="development",
        environment_id="11111111-1111-4111-8111-111111111111",
        business_settings=business,
        setup_ready=setup_ready,
        static_resources=static_resources,
        assembly=assembly,
        recovery=recovery,
        credential_backend_available=credential_backend_available,
    )


def _probes_from(overrides: dict[str, ProbeOutcome]) -> DependencyProbes:
    """按名称覆盖单项结果，其余保持 ok。"""

    return _probes(
        postgres=overrides.get("postgres", OK),
        vector=overrides.get("vector", OK),
        migration=overrides.get("migration", OK),
        schema=overrides.get("schema", OK),
        redis=overrides.get("redis", OK),
        storage=overrides.get("storage", OK),
    )


def _check(report: ReadinessReport, name: str) -> ProbeOutcome:
    attribute = "schema_check" if name == "schema" else name
    return getattr(report.business_prerequisites.checks, attribute)


# ---------------------------------------------------------------------------
# 分层语义
# ---------------------------------------------------------------------------


def test_all_layers_ready_when_dependencies_and_startup_complete() -> None:
    report = evaluate_readiness(_inputs(), _probes())

    assert report.setup.ready is True
    assert report.business_prerequisites.ready is True
    assert report.business.assembly == "mounted"
    assert report.business.recovery == "completed"
    assert report.business.ready is True
    assert report.business.reason is None
    assert gate_ready(report) is None
    assert report.static_resources == "available"
    assert report.restart_required is False


def test_prerequisites_pass_without_assembly_is_not_business_ready() -> None:
    report = evaluate_readiness(_inputs(assembly="not_assembled"), _probes())

    assert report.business_prerequisites.ready is True
    assert report.business.ready is False
    assert report.business.assembly == "not_mounted"
    assert report.business.reason == "business_not_mounted"
    assert gate_ready(report) == ("assembly", "业务入口尚未装配。")


def test_recovery_stage_gates_business_ready() -> None:
    not_started = evaluate_readiness(_inputs(recovery="not_started"), _probes())
    assert not_started.business.ready is False
    assert gate_ready(not_started) == ("recovery", "启动恢复阶段尚未完成。")

    failed = evaluate_readiness(_inputs(recovery="failed"), _probes())
    assert failed.business.reason == "recovery_failed"
    assert gate_ready(failed) == ("recovery", "启动恢复阶段失败。")


def test_business_config_missing_skips_all_dependency_probes() -> None:
    calls: list[str] = []
    report = evaluate_readiness(
        _inputs(configured=False, assembly="not_assembled"),
        _probes(calls=calls),
    )

    assert calls == []
    assert report.setup.ready is True
    assert report.business_prerequisites.ready is False
    assert _check(report, "business_config").code == "business_config_missing"
    for name in ("postgres", "vector", "migration", "schema", "redis", "storage"):
        assert _check(report, name).status == "skipped"
    assert gate_ready(report) == ("assembly", "业务入口尚未装配。")


def test_ai_section_is_never_evaluated() -> None:
    for inputs in (_inputs(), _inputs(configured=False, assembly="not_assembled")):
        report = evaluate_readiness(inputs, _probes())

        assert report.ai.evaluated is False
        assert report.ai.status == "not_evaluated"
        assert report.ai.reason == "not_evaluated"
        assert report.ai.tasks == {}


def test_unknown_migration_head_blocks_business_readiness() -> None:
    """T220：按 plan §5.1「允许的迁移 head…均通过」，unknown 不得放行。"""

    report = evaluate_readiness(_inputs(), _probes(migration=UNKNOWN_MIGRATION))

    assert _check(report, "migration").status == "unknown"
    assert _check(report, "migration").code == "migration_head_unavailable"
    assert report.business_prerequisites.ready is False
    assert report.business.ready is False
    assert gate_ready(report) == ("migrations", "未判断版本一致性。")


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probe_overrides", "expected_failed_check"),
    [
        ({"postgres": FAILED_POSTGRES}, "postgres"),
        ({"vector": FAILED_VECTOR}, "business_prerequisites"),
        ({"migration": FAILED_MIGRATION}, "migrations"),
        ({"schema": FAILED_SCHEMA}, "business_prerequisites"),
        ({"redis": FAILED_REDIS}, "redis"),
        ({"storage": FAILED_STORAGE}, "storage"),
    ],
)
def test_prerequisite_failures_map_to_stable_ready_classes(
    probe_overrides: dict[str, ProbeOutcome], expected_failed_check: str
) -> None:
    report = evaluate_readiness(_inputs(), _probes_from(probe_overrides))

    assert report.business_prerequisites.ready is False
    assert report.business.ready is False
    failure = gate_ready(report)
    assert failure is not None
    assert failure[0] == expected_failed_check
    assert failure[1]


def test_retired_jwt_does_not_gate_personal_readiness() -> None:
    settings = cast(
        AppSettings,
        type(
            "FakeSettings",
            (),
            {"JWT_SECRET_KEY": None, "storage_root": Path(".")},
        )(),
    )

    report = evaluate_readiness(_inputs(settings=settings), _probes())

    assert _check(report, "jwt_reference").code == "retired_business_auth"
    assert _check(report, "jwt_reference").status == "skipped"
    assert gate_ready(report) is None


def test_missing_credential_backend_never_falls_back_to_anonymous() -> None:
    report = evaluate_readiness(
        _inputs(credential_backend_available=False), _probes()
    )

    check = _check(report, "business_config")
    assert check.status == "failed"
    assert check.code == "credential_backend_unavailable"
    assert gate_ready(report) == ("config", "凭据后端不可用，未降级为匿名连接。")


# ---------------------------------------------------------------------------
# 受限诊断
# ---------------------------------------------------------------------------


def test_diagnostics_only_runs_allowed_checks_in_one_database_pass() -> None:
    calls: list[str] = []
    report: DiagnosticsReport = run_diagnostics(
        _inputs(),
        ("postgres", "migrations", "redis", "storage", "unknown-check"),
        _probes(calls=calls),
        evaluated_at=datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    )

    assert calls == ["database", "redis", "storage"]
    assert set(report.checks) == {"postgres", "migrations", "redis", "storage"}
    assert report.status == "ok"
    assert report.evaluated_at == datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def test_diagnostics_is_degraded_on_failure_and_skipped_without_config() -> None:
    failing = run_diagnostics(_inputs(), ("postgres",), _probes(postgres=FAILED_POSTGRES))
    assert failing.status == "degraded"
    assert failing.checks["postgres"].code == "postgres_unavailable"

    skipped = run_diagnostics(_inputs(configured=False), ("postgres", "storage"))
    assert skipped.status == "degraded"
    assert skipped.checks["postgres"].status == "skipped"
    assert skipped.checks["storage"].status == "skipped"


# ---------------------------------------------------------------------------
# 默认探针：资源释放与安全结果
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def first(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


@dataclass
class _FakeConnection:
    version: str | None = "0024_head"
    tables: tuple[str, ...] = ("users", "courses")
    vector_installed: bool = True

    def execute(self, statement: object) -> _Rows:
        sql = str(statement)
        if "pg_extension" in sql:
            return _Rows([(1,)] if self.vector_installed else [])
        if "alembic_version" in sql:
            return _Rows([(self.version,)] if self.version else [])
        if "information_schema" in sql:
            return _Rows([(name,) for name in self.tables])
        return _Rows([(1,)])

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> _FakeConnection:
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


@pytest.fixture()
def fake_database(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"connection": _FakeConnection(), "engine": None}

    def factory(*_args: object, **_kwargs: object) -> _FakeEngine:
        engine = _FakeEngine(cast(_FakeConnection, state["connection"]))
        state["engine"] = engine
        return engine

    monkeypatch.setattr(readiness, "create_database_engine", factory)
    monkeypatch.setattr(readiness, "_required_tables", lambda: frozenset({"users", "courses"}))
    return state


def test_default_database_probe_releases_engine_and_reports_ok(
    fake_database: dict[str, object],
) -> None:
    result = probe_database(build_test_settings(), "0024_head")

    assert result.postgres.status == "ok"
    assert result.vector.status == "ok"
    assert result.migration.status == "ok"
    assert result.schema.status == "ok"
    engine = cast(_FakeEngine, fake_database["engine"])
    assert engine.disposed is True


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        (_FakeConnection(version="0023_old"), "migration_outdated"),
        (_FakeConnection(version=None), "migration_missing"),
        (_FakeConnection(vector_installed=False), "vector_missing"),
        (_FakeConnection(tables=("users",)), "schema_drift"),
    ],
)
def test_default_database_probe_reports_drift_with_stable_codes(
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeConnection,
    expected: str,
) -> None:
    engine = _FakeEngine(connection)
    monkeypatch.setattr(readiness, "create_database_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(readiness, "_required_tables", lambda: frozenset({"users", "courses"}))

    result = probe_database(build_test_settings(), "0024_head")

    codes = {result.migration.code, result.vector.code, result.schema.code}
    assert expected in codes
    assert engine.disposed is True


def test_default_database_probe_degrades_safely_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenEngine:
        def __init__(self) -> None:
            self.disposed = False

        def connect(self) -> object:
            raise SQLAlchemyError("postgresql://user:secret@127.0.0.1/db")

        def dispose(self) -> None:
            self.disposed = True

    engine = _BrokenEngine()
    monkeypatch.setattr(readiness, "create_database_engine", lambda *_a, **_k: engine)

    result = probe_database(build_test_settings())

    assert result.postgres.code == "postgres_unavailable"
    assert result.vector.status == "skipped"
    assert engine.disposed is True
    assert "secret" not in str(result.postgres)


def test_default_redis_probe_closes_client_and_redacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {"closed": False}

    class _FakeRedis:
        def ping(self) -> bool:
            raise RedisError("redis://:secret@127.0.0.1:6379/0")

        def close(self) -> None:
            state["closed"] = True

    monkeypatch.setattr(readiness, "create_redis_client", lambda *_a, **_k: _FakeRedis())

    outcome = probe_redis(build_test_settings())

    assert outcome.code == "redis_unavailable"
    assert state["closed"] is True
    assert "secret" not in str(outcome)


def test_default_storage_probe_creates_and_removes_only_its_own_file(
    tmp_path: Path,
) -> None:
    settings = build_test_settings(STORAGE_ROOT=tmp_path)

    outcome = probe_storage(settings)

    assert outcome.status == "ok"
    assert list(tmp_path.iterdir()) == []

    missing = probe_storage(build_test_settings(STORAGE_ROOT=tmp_path / "missing"))
    assert missing.code == "storage_unavailable"


def test_storage_probe_reports_unwritable_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = build_test_settings(STORAGE_ROOT=tmp_path)

    def unwritable(*_args: object, **_kwargs: object) -> int:
        raise OSError(f"cannot write {tmp_path}")

    monkeypatch.setattr(Path, "write_text", unwritable)

    outcome = probe_storage(settings)

    assert outcome.code == "storage_unwritable"
    assert str(tmp_path) not in str(outcome)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 迁移头发现
# ---------------------------------------------------------------------------


def _migration(directory: Path, name: str, revision: str, down: str | None) -> None:
    down_line = "None" if down is None else f'"{down}"'
    (directory / f"{name}.py").write_text(
        f'revision: str = "{revision}"\ndown_revision: str | None = {down_line}\n',
        encoding="utf-8",
    )


def test_migration_head_is_discovered_from_files(tmp_path: Path) -> None:
    _migration(tmp_path, "0001_a", "0001_a", None)
    _migration(tmp_path, "0002_b", "0002_b", "0001_a")

    assert discover_migration_head(tmp_path) == "0002_b"


def test_ambiguous_or_missing_migration_directory_returns_none(tmp_path: Path) -> None:
    _migration(tmp_path, "0001_a", "0001_a", None)
    _migration(tmp_path, "0002_b", "0002_b", None)

    assert discover_migration_head(tmp_path) is None
    assert discover_migration_head(tmp_path / "missing") is None
