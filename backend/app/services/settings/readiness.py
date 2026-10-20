"""分层就绪与核心依赖诊断（T219）。

只读边界：不写配置、不改业务数据、不调用 AI/Probe/模型，也不依赖 Registry 声明能力。
`storage` 探针仅创建并删除自身临时文件；数据库探针在单一连接内完成
postgres/vector/migration/schema 检查，并在 finally 中释放 engine。

四层语义：

- `setup`：静态资源/最小配置/设置授权服务是否可用（与 `/setup-ready` 同源事实）；
- `business_prerequisites`：业务配置、JWT 引用、PostgreSQL、pgvector、迁移、表结构、
  Redis、Storage；
- `business`：前置依赖 + 装配（not_mounted/mounted/failed）+ 本进程原恢复启动阶段；
- `ai`：早期 E1 固定 `evaluated=false`/`not_evaluated`，不伪造任何模型可用性。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import Base, create_database_engine
from backend.app.core.redis import create_redis_client
from backend.app.schemas.settings import (
    AiReadiness,
    BusinessPrerequisitesReadiness,
    BusinessReadiness,
    DiagnosticsReport,
    ReadinessCheck,
    ReadinessChecks,
    ReadinessReport,
    SetupReadiness,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from backend.app.core.config import AppSettings

DIAGNOSTIC_CHECKS = ("postgres", "redis", "storage", "migrations")
PREREQUISITE_ORDER = (
    "business_config",
    "jwt_reference",
    "postgres",
    "vector",
    "migration",
    "schema",
    "redis",
    "storage",
)
#: 分项失败 → 既有 `/ready` 分类（不新增公开不兼容码）。
FAILED_CHECK_BY_PREREQUISITE = {
    "business_config": "config",
    "jwt_reference": "business_prerequisites",
    "postgres": "postgres",
    "vector": "business_prerequisites",
    "migration": "migrations",
    "schema": "business_prerequisites",
    "redis": "redis",
    "storage": "storage",
}

ProbeStatus = Literal["ok", "failed", "unknown", "skipped"]

#: `schema` 与 pydantic 废弃方法名冲突，模型侧属性为 `schema_check`。
_CHECK_ATTRIBUTE = {"schema": "schema_check"}

_REVISION_PATTERN = re.compile(
    r"^revision(?::\s*[^=]+)?\s*=\s*[\"']([A-Za-z0-9_]+)[\"']", re.MULTILINE
)
_DOWN_REVISION_PATTERN = re.compile(
    r"^down_revision(?::\s*[^=]+)?\s*=\s*(.+)$", re.MULTILINE
)
_QUOTED_NAME = re.compile(r"[\"']([A-Za-z0-9_]+)[\"']")


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """单个检查的安全结果；`detail` 只允许固定文本。"""

    status: ProbeStatus
    code: str | None = None
    detail: str | None = None

    def as_check(self) -> ReadinessCheck:
        return ReadinessCheck(status=self.status, code=self.code, detail=self.detail)


OK = ProbeOutcome("ok")
SKIPPED_WITHOUT_CONFIG = ProbeOutcome(
    "skipped",
    code="business_config_missing",
    detail="缺少业务配置，未执行该检查。",
)
FAILED_POSTGRES = ProbeOutcome(
    "failed", code="postgres_unavailable", detail="PostgreSQL 未就绪。"
)
SKIPPED_AFTER_POSTGRES = ProbeOutcome(
    "skipped",
    code="postgres_unavailable",
    detail="PostgreSQL 未就绪，未执行该检查。",
)
FAILED_REDIS = ProbeOutcome("failed", code="redis_unavailable", detail="Redis 未就绪。")
FAILED_STORAGE = ProbeOutcome(
    "failed", code="storage_unavailable", detail="存储目录不可用。"
)


@dataclass(frozen=True, slots=True)
class DatabaseProbeResult:
    postgres: ProbeOutcome
    vector: ProbeOutcome
    migration: ProbeOutcome
    schema: ProbeOutcome


@dataclass(frozen=True, slots=True)
class DependencyProbes:
    """可注入的依赖探针；单元测试用假实现，默认实现自持资源并释放。"""

    database: Callable[[AppSettings, str | None], DatabaseProbeResult]
    redis: Callable[[AppSettings], ProbeOutcome]
    storage: Callable[[AppSettings], ProbeOutcome]


# ---------------------------------------------------------------------------
# 默认探针（现有基础设施，不改其生产语义）
# ---------------------------------------------------------------------------


def migrations_directory() -> Path:
    """仓库/安装目录内的迁移目录；不存在时由调用方降级为 unknown。"""

    return Path(__file__).resolve().parents[4] / "migrations" / "versions"


def discover_migration_head(directory: Path | None = None) -> str | None:
    """从迁移文件推导唯一 head；无法唯一确定时返回 None（不猜版本）。"""

    target = directory if directory is not None else migrations_directory()
    if not target.is_dir():
        return None
    revisions: set[str] = set()
    parents: set[str] = set()
    try:
        files = sorted(target.glob("*.py"))
    except OSError:
        return None
    for file in files:
        try:
            content = file.read_text(encoding="utf-8")
        except OSError:
            continue
        revisions.update(_REVISION_PATTERN.findall(content))
        for line in _DOWN_REVISION_PATTERN.findall(content):
            parents.update(_QUOTED_NAME.findall(line))
    heads = revisions - parents
    if len(heads) != 1:
        return None
    return heads.pop()


def _required_tables() -> frozenset[str]:
    """延迟导入业务模型只为读取表名；不连接数据库。"""

    try:
        from backend.app import models  # noqa: F401  仅用于填充 Base.metadata
    except ImportError:
        return frozenset()
    return frozenset(Base.metadata.tables)


def _probe_vector(connection: Connection) -> ProbeOutcome:
    try:
        row = connection.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).first()
    except SQLAlchemyError:
        return ProbeOutcome(
            "failed", code="vector_unavailable", detail="无法确认 pgvector 扩展状态。"
        )
    if row is None:
        return ProbeOutcome(
            "failed", code="vector_missing", detail="pgvector 扩展未安装。"
        )
    return OK


def _probe_migration(connection: Connection, expected_head: str | None) -> ProbeOutcome:
    try:
        row = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).first()
    except SQLAlchemyError:
        return ProbeOutcome(
            "failed", code="migration_missing", detail="数据库迁移版本表不可用。"
        )
    if row is None:
        return ProbeOutcome(
            "failed", code="migration_missing", detail="数据库缺少迁移版本记录。"
        )
    current = str(row[0])
    if not expected_head:
        return ProbeOutcome(
            "unknown",
            code="migration_head_unavailable",
            detail="无法从本进程读取迁移头，未判断版本一致性。",
        )
    if current != expected_head:
        return ProbeOutcome(
            "failed", code="migration_outdated", detail="数据库迁移版本与代码不一致。"
        )
    return OK


def _probe_schema(connection: Connection) -> ProbeOutcome:
    required = _required_tables()
    if not required:
        return ProbeOutcome(
            "unknown",
            code="schema_reference_unavailable",
            detail="本进程未加载业务表定义，未判断表结构。",
        )
    try:
        rows = connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
        ).all()
    except SQLAlchemyError:
        return ProbeOutcome(
            "failed", code="schema_unavailable", detail="无法读取数据库表结构。"
        )
    existing = {str(row[0]) for row in rows}
    missing = required - existing
    if missing:
        return ProbeOutcome(
            "failed",
            code="schema_drift",
            detail=f"缺少 {len(missing)} 张必需业务表。",
        )
    return OK


def probe_database(
    settings: AppSettings,
    expected_head: str | None = None,
    *,
    connect_timeout: int = 2,
) -> DatabaseProbeResult:
    """单连接完成四项数据库检查；无论成败都释放 engine。"""

    engine = create_database_engine(settings, connect_timeout=connect_timeout)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            return DatabaseProbeResult(
                postgres=OK,
                vector=_probe_vector(connection),
                migration=_probe_migration(
                    connection,
                    expected_head if expected_head else discover_migration_head(),
                ),
                schema=_probe_schema(connection),
            )
    except SQLAlchemyError:
        return DatabaseProbeResult(
            postgres=FAILED_POSTGRES,
            vector=SKIPPED_AFTER_POSTGRES,
            migration=SKIPPED_AFTER_POSTGRES,
            schema=SKIPPED_AFTER_POSTGRES,
        )
    finally:
        engine.dispose()


def probe_redis(settings: AppSettings) -> ProbeOutcome:
    client = create_redis_client(
        settings, socket_connect_timeout=1, socket_timeout=1
    )
    try:
        client.ping()
    except (RedisError, OSError, TimeoutError):
        return ProbeOutcome("failed", code="redis_unavailable", detail="Redis 未就绪。")
    finally:
        client.close()
    return OK


def probe_storage(settings: AppSettings) -> ProbeOutcome:
    """只创建并清理本次探针文件；不写配置、不动业务数据。"""

    root = Path(settings.storage_root)
    if not root.is_dir():
        return ProbeOutcome(
            "failed", code="storage_unavailable", detail="存储目录不可用。"
        )
    probe_file = root / f".readiness-{uuid4().hex}.probe"
    try:
        probe_file.write_text("ok", encoding="utf-8")
    except OSError:
        return ProbeOutcome(
            "failed", code="storage_unwritable", detail="存储目录不可写。"
        )
    finally:
        try:
            probe_file.unlink(missing_ok=True)
        except OSError:
            pass
    return OK


DEFAULT_PROBES = DependencyProbes(
    database=probe_database, redis=probe_redis, storage=probe_storage
)


# ---------------------------------------------------------------------------
# 分层评估
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadinessInputs:
    """一次评估所需的全部事实；不在此处触发任何 I/O。"""

    environment: str | None = None
    environment_id: str | None = None
    business_settings: AppSettings | None = None
    setup_ready: bool = False
    setup_reason: str | None = None
    static_resources: str = "not_bundled"
    assembly: str = "not_assembled"
    recovery: str = "not_started"
    configuration_revision: str | None = None
    pending_revision: str | None = None
    expected_migration_head: str | None = None
    credential_backend_available: bool | None = None


def inputs_for_app(
    app: FastAPI, *, business_settings: AppSettings | None = None
) -> ReadinessInputs:
    """从应用状态读取引导身份/装配/恢复事实；不回退成业务配置。"""

    context = getattr(app.state, "bootstrap", None)
    bootstrap_settings = getattr(context, "settings", None) if context is not None else None
    static_resources = (
        getattr(context, "static_resources", "not_bundled")
        if context is not None
        else "not_bundled"
    )
    setup_ready = (
        bootstrap_settings is not None
        and getattr(app.state, "setup_security", None) is not None
    )
    return ReadinessInputs(
        environment=(
            bootstrap_settings.environment.value
            if bootstrap_settings is not None
            else None
        ),
        environment_id=(
            str(bootstrap_settings.environment_id)
            if bootstrap_settings is not None
            else None
        ),
        business_settings=business_settings,
        setup_ready=setup_ready,
        setup_reason=None if setup_ready else "setup_unavailable",
        static_resources=static_resources,
        assembly=getattr(app.state, "business_assembly", "not_assembled"),
        recovery=getattr(app.state, "recovery", "not_started"),
        configuration_revision=getattr(app.state, "configuration_revision", None),
        pending_revision=getattr(app.state, "pending_revision", None),
        expected_migration_head=getattr(app.state, "migration_head", None),
        credential_backend_available=getattr(
            app.state, "credential_backend_available", None
        ),
    )


def _jwt_reference(settings: AppSettings) -> ProbeOutcome:
    secret = settings.JWT_SECRET_KEY
    value: object = (
        secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    )
    if not isinstance(value, str) or not value.strip():
        return ProbeOutcome(
            "failed",
            code="jwt_reference_missing",
            detail="业务 JWT 密钥引用缺失。",
        )
    return OK


def _run_database_probe(
    probes: DependencyProbes, settings: AppSettings, expected_head: str | None
) -> DatabaseProbeResult:
    """探针自身抛出时也不向调用方泄异常原文。"""

    try:
        return probes.database(settings, expected_head)
    except Exception:  # noqa: BLE001 - 探针不得把异常类型/原文带出就绪评估
        return DatabaseProbeResult(
            postgres=FAILED_POSTGRES,
            vector=SKIPPED_AFTER_POSTGRES,
            migration=SKIPPED_AFTER_POSTGRES,
            schema=SKIPPED_AFTER_POSTGRES,
        )


def _run_redis_probe(probes: DependencyProbes, settings: AppSettings) -> ProbeOutcome:
    try:
        return probes.redis(settings)
    except Exception:  # noqa: BLE001 - 同上，统一收敛为安全失败码
        return FAILED_REDIS


def _run_storage_probe(probes: DependencyProbes, settings: AppSettings) -> ProbeOutcome:
    try:
        return probes.storage(settings)
    except Exception:  # noqa: BLE001 - 同上，统一收敛为安全失败码
        return FAILED_STORAGE


def _prerequisite_checks(
    inputs: ReadinessInputs, probes: DependencyProbes
) -> dict[str, ProbeOutcome]:
    settings = inputs.business_settings
    if settings is None:
        missing = ProbeOutcome(
            "failed",
            code="business_config_missing",
            detail="业务配置尚未提供；设置面仍可用。",
        )
        return {
            "business_config": missing,
            "jwt_reference": SKIPPED_WITHOUT_CONFIG,
            "postgres": SKIPPED_WITHOUT_CONFIG,
            "vector": SKIPPED_WITHOUT_CONFIG,
            "migration": SKIPPED_WITHOUT_CONFIG,
            "schema": SKIPPED_WITHOUT_CONFIG,
            "redis": SKIPPED_WITHOUT_CONFIG,
            "storage": SKIPPED_WITHOUT_CONFIG,
        }
    config = (
        ProbeOutcome(
            "failed",
            code="credential_backend_unavailable",
            detail="凭据后端不可用，未降级为匿名连接。",
        )
        if inputs.credential_backend_available is False
        else OK
    )
    database = _run_database_probe(probes, settings, inputs.expected_migration_head)
    # PostgreSQL 不可用时其余依赖无意义：保持短失败，但报告仍列出分项。
    postgres_ready = database.postgres.status != "failed"
    return {
        "business_config": config,
        "jwt_reference": _jwt_reference(settings),
        "postgres": database.postgres,
        "vector": database.vector,
        "migration": database.migration,
        "schema": database.schema,
        "redis": (
            _run_redis_probe(probes, settings)
            if postgres_ready
            else SKIPPED_AFTER_POSTGRES
        ),
        "storage": (
            _run_storage_probe(probes, settings)
            if postgres_ready
            else SKIPPED_AFTER_POSTGRES
        ),
    }


def assembly_state(value: str) -> Literal["not_mounted", "mounted", "failed"]:
    if value == "assembled":
        return "mounted"
    if value == "failed":
        return "failed"
    return "not_mounted"


def _recovery_state(value: str) -> Literal["not_started", "completed", "failed"]:
    if value == "completed":
        return "completed"
    if value == "failed":
        return "failed"
    return "not_started"


def evaluate_readiness(
    inputs: ReadinessInputs,
    probes: DependencyProbes | None = None,
    *,
    ai: AiReadiness | None = None,
) -> ReadinessReport:
    """按层次汇总事实；AI 段只接受已计算的逐任务事实。"""

    active_probes = probes if probes is not None else DEFAULT_PROBES
    outcomes = _prerequisite_checks(inputs, active_probes)
    checks = ReadinessChecks(
        **{name: outcomes[name].as_check() for name in PREREQUISITE_ORDER}
    )
    # 「均通过」不包含 unknown：无法判定迁移头/表结构时业务不应宣称就绪。
    prerequisites_ready = all(outcome.status == "ok" for outcome in outcomes.values())
    assembly = assembly_state(inputs.assembly)
    recovery = _recovery_state(inputs.recovery)
    business_ready = (
        prerequisites_ready and assembly == "mounted" and recovery == "completed"
    )
    if business_ready:
        business_reason = None
    elif assembly != "mounted":
        business_reason = "business_not_mounted"
    elif not prerequisites_ready:
        business_reason = "prerequisites_not_ready"
    elif recovery == "failed":
        business_reason = "recovery_failed"
    else:
        business_reason = "recovery_not_completed"
    return ReadinessReport(
        environment=inputs.environment,
        setup=SetupReadiness(ready=inputs.setup_ready, reason=inputs.setup_reason),
        business_prerequisites=BusinessPrerequisitesReadiness(
            ready=prerequisites_ready,
            configuration_revision=inputs.configuration_revision,
            checks=checks,
        ),
        business=BusinessReadiness(
            ready=business_ready,
            assembly=assembly,
            recovery=recovery,
            reason=business_reason,
        ),
        ai=ai if ai is not None else AiReadiness(),
        static_resources=inputs.static_resources,
        restart_required=inputs.pending_revision is not None,
        pending_revision=inputs.pending_revision,
        candidate_diagnostics=None,
    )


def prerequisites_failure(report: ReadinessReport) -> tuple[str, str] | None:
    """业务前置条件门禁：每一项都必须 ``ok``，`unknown` 也闭锁。"""

    for name in PREREQUISITE_ORDER:
        check = getattr(
            report.business_prerequisites.checks, _CHECK_ATTRIBUTE.get(name, name)
        )
        if check.status != "ok":
            return (
                FAILED_CHECK_BY_PREREQUISITE[name],
                check.detail or "业务前置条件未就绪。",
            )
    return None


def gate_ready(report: ReadinessReport) -> tuple[str, str] | None:
    """`/ready` 通过条件：装配 + 全部前置条件 + 恢复阶段完成。"""

    if report.business.assembly != "mounted":
        return ("assembly", "业务入口尚未装配。")
    failure = prerequisites_failure(report)
    if failure is not None:
        return failure
    if report.business.recovery == "failed":
        return ("recovery", "启动恢复阶段失败。")
    if report.business.recovery != "completed":
        return ("recovery", "启动恢复阶段尚未完成。")
    if not report.business.ready:
        return ("business", "业务入口未达到就绪状态。")
    return None


# ---------------------------------------------------------------------------
# 受限诊断
# ---------------------------------------------------------------------------


def run_diagnostics(
    inputs: ReadinessInputs,
    checks: tuple[str, ...],
    probes: DependencyProbes | None = None,
    *,
    evaluated_at: datetime | None = None,
) -> DiagnosticsReport:
    """只允许 `DIAGNOSTIC_CHECKS` 内的即时探测，不做自动修复。"""

    active_probes = probes if probes is not None else DEFAULT_PROBES
    settings = inputs.business_settings
    requested = [name for name in checks if name in DIAGNOSTIC_CHECKS]
    database: DatabaseProbeResult | None = None
    if settings is not None and any(
        name in {"postgres", "migrations"} for name in requested
    ):
        database = _run_database_probe(
            active_probes, settings, inputs.expected_migration_head
        )
    results: dict[str, ProbeOutcome] = {}
    for name in requested:
        if name == "postgres":
            results[name] = database.postgres if database is not None else SKIPPED_WITHOUT_CONFIG
        elif name == "migrations":
            results[name] = (
                database.migration if database is not None else SKIPPED_WITHOUT_CONFIG
            )
        elif name == "redis":
            results[name] = (
                _run_redis_probe(active_probes, settings)
                if settings is not None
                else SKIPPED_WITHOUT_CONFIG
            )
        elif name == "storage":
            results[name] = (
                _run_storage_probe(active_probes, settings)
                if settings is not None
                else SKIPPED_WITHOUT_CONFIG
            )
    status: Literal["ok", "degraded"] = (
        "degraded"
        if any(item.status in {"failed", "skipped"} for item in results.values())
        else "ok"
    )
    return DiagnosticsReport(
        status=status,
        checks={name: outcome.as_check() for name, outcome in results.items()},
        evaluated_at=evaluated_at or datetime.now(UTC),
    )


__all__ = [
    "DEFAULT_PROBES",
    "DIAGNOSTIC_CHECKS",
    "FAILED_CHECK_BY_PREREQUISITE",
    "PREREQUISITE_ORDER",
    "DatabaseProbeResult",
    "DependencyProbes",
    "ProbeOutcome",
    "ReadinessInputs",
    "assembly_state",
    "discover_migration_head",
    "evaluate_readiness",
    "gate_ready",
    "inputs_for_app",
    "migrations_directory",
    "prerequisites_failure",
    "probe_database",
    "probe_redis",
    "probe_storage",
    "run_diagnostics",
]
