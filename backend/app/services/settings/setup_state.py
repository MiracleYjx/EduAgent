"""T088/T089: read-only setup recommendations and existing-data detection.

This module never writes configuration, drafts, databases, directories or
credentials. It only projects non-secret runtime facts and performs bounded
read-only probes. Failures are converted to stable codes; connection strings,
passwords and file names never leave this boundary.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from backend.app.schemas.settings import (
    BusinessSettings,
    ConfigurationValues,
    DatabaseLocator,
    RedisLocator,
    SetupDatabaseData,
    SetupDataState,
    SetupRecommendation,
    SetupRecommendedBusiness,
    SetupStorageData,
)

#: Current personal-learning tables; used only for read-only classification.
_BUSINESS_TABLES = frozenset(
    {
        "subjects",
        "knowledge_points",
        "material_series",
        "material_versions",
        "questions",
        "question_revisions",
        "question_origins",
        "question_assets",
        "paper_imports",
        "source_pages",
        "extracted_questions",
        "practice_sessions",
        "practice_items",
        "practice_answers",
        "grade_attempts",
        "score_disputes",
        "conversations",
        "conversation_messages",
        "operation_runs",
        "alembic_version",
    }
)

#: Fixed table names only; never interpolate a table name discovered from SQL.
_ROW_EVIDENCE_TABLES = (
    "subjects",
    "questions",
    "practice_sessions",
    "conversations",
    "operation_runs",
)

_DB_SOURCE = "business.database_locator"
_REDIS_SOURCE = "business.redis_locator"
_STORAGE_SOURCE = "business.storage_root"
_DEV_MODE_SOURCE = "business.dev_mode"
_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off"})


def _parse_bool(raw: str) -> bool | None:
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def _mode(runtime_kind: object) -> Literal["docker", "python", "unknown"]:
    value = getattr(runtime_kind, "value", runtime_kind)
    if value == "docker":
        return "docker"
    if value in {"python", "windows", "source", "test"}:
        return "python"
    return "unknown"


def _parse_database_url(raw: str) -> tuple[DatabaseLocator, bool] | None:
    try:
        url = make_url(raw)
        if not url.drivername.startswith("postgresql"):
            return None
        if not url.host or not url.database or not url.username:
            return None
        locator = DatabaseLocator(
            host=url.host,
            port=url.port or 5432,
            database=url.database,
            user=url.username,
        )
    except (SQLAlchemyError, ValueError, TypeError):
        return None
    return locator, url.password is not None


def _parse_redis_url(raw: str) -> tuple[RedisLocator, bool] | None:
    try:
        url = make_url(raw)
        if not url.drivername.startswith("redis") or not url.host:
            return None
        raw_database = str(url.database or "0")
        if not raw_database.isdigit():
            return None
        locator = RedisLocator(
            host=url.host,
            port=url.port or 6379,
            database=int(raw_database),
        )
    except (SQLAlchemyError, ValueError, TypeError):
        return None
    return locator, url.password is not None


def _absolute_path(raw: str) -> Path | None:
    try:
        path = Path(raw)
        # Tests often run on Windows while Docker recommendations are POSIX.
        posix_absolute = PurePosixPath(raw).is_absolute()
    except (OSError, ValueError, TypeError):
        return None
    return path if path.is_absolute() or posix_absolute else None


def build_setup_recommendation(
    *,
    active: ConfigurationValues | None,
    runtime_kind: object,
    environ: Mapping[str, str],
    profile_database: DatabaseLocator | None = None,
    profile_redis: RedisLocator | None = None,
    profile_storage: Path | None = None,
) -> SetupRecommendation:
    """Build a non-secret recommendation without writing any configuration."""

    current = active.business if active is not None else BusinessSettings()
    database = current.database_locator
    redis = current.redis_locator
    storage = current.storage_root
    sources: dict[str, str] = {}
    warnings: list[str] = []
    blocking = False

    if database is not None:
        sources[_DB_SOURCE] = "active_configuration"
        if current.database_password_ref is not None:
            warnings.append("DATABASE_PASSWORD_REF_PRESENT")
            blocking = True
    else:
        raw = environ.get("DATABASE_URL", "").strip()
        parsed_database = _parse_database_url(raw) if raw else None
        if parsed_database is not None:
            database, has_password = parsed_database
            sources[_DB_SOURCE] = "process_env"
            if has_password:
                warnings.append("DATABASE_PASSWORD_NOT_INCLUDED")
                blocking = True
        elif raw:
            warnings.append("INVALID_DATABASE_URL")
            blocking = True
        elif profile_database is not None:
            database = profile_database
            sources[_DB_SOURCE] = "environment_profile"
        else:
            sources[_DB_SOURCE] = "unavailable"

    if redis is not None:
        sources[_REDIS_SOURCE] = "active_configuration"
        if current.redis_password_ref is not None:
            warnings.append("REDIS_PASSWORD_REF_PRESENT")
            blocking = True
    else:
        raw = environ.get("REDIS_URL", "").strip()
        parsed_redis = _parse_redis_url(raw) if raw else None
        if parsed_redis is not None:
            redis, has_password = parsed_redis
            sources[_REDIS_SOURCE] = "process_env"
            if has_password:
                warnings.append("REDIS_PASSWORD_NOT_INCLUDED")
                blocking = True
        elif raw:
            warnings.append("INVALID_REDIS_URL")
            blocking = True
        elif profile_redis is not None:
            redis = profile_redis
            sources[_REDIS_SOURCE] = "environment_profile"
        else:
            sources[_REDIS_SOURCE] = "unavailable"

    if storage is not None:
        sources[_STORAGE_SOURCE] = "active_configuration"
    else:
        raw = environ.get("STORAGE_ROOT", "").strip()
        parsed_storage = _absolute_path(raw) if raw else None
        if parsed_storage is not None:
            storage = parsed_storage
            sources[_STORAGE_SOURCE] = "process_env"
        elif raw:
            warnings.append("STORAGE_ROOT_NOT_ABSOLUTE")
            blocking = True
        elif profile_storage is not None:
            storage = profile_storage
            sources[_STORAGE_SOURCE] = "environment_profile"
        else:
            sources[_STORAGE_SOURCE] = "unavailable"

    dev_mode = current.dev_mode
    invalid_dev_mode = False
    raw_dev_mode = environ.get("DEV_MODE")
    if raw_dev_mode is not None and raw_dev_mode.strip():
        parsed_dev_mode = _parse_bool(raw_dev_mode)
        if parsed_dev_mode is None:
            warnings.append("INVALID_DEV_MODE")
            invalid_dev_mode = True
        elif active is None:
            dev_mode = parsed_dev_mode
        if active is None:
            sources[_DEV_MODE_SOURCE] = "process_env"
    if active is not None:
        sources[_DEV_MODE_SOURCE] = "active_configuration"

    missing = []
    if database is None:
        missing.append("DATABASE")
    if redis is None:
        missing.append("REDIS")
    if storage is None:
        missing.append("STORAGE")
    available = not missing
    reason: str | None = None
    if missing:
        reason = f"{missing[0]}_NOT_CONFIGURED"
    elif blocking:
        reason = "CREDENTIAL_OR_SOURCE_REQUIRED"
    elif invalid_dev_mode:
        reason = "INVALID_DEV_MODE"

    return SetupRecommendation(
        available=available,
        auto_apply=available and not blocking and not invalid_dev_mode,
        mode=_mode(runtime_kind),
        business=SetupRecommendedBusiness(
            database_locator=database,
            redis_locator=redis,
            storage_root=storage,
            dev_mode=dev_mode,
        ),
        sources=sources,
        reason=reason,
        warnings=tuple(warnings),
    )


def probe_database_data(locator: DatabaseLocator | None) -> SetupDatabaseData:
    """Read table metadata and a few fixed key tables; never mutate the DB."""

    if locator is None:
        return SetupDatabaseData(
            status="unavailable",
            code="DATABASE_NOT_CONFIGURED",
            detail="尚未检测到可用的数据库配置。",
        )

    engine = None
    try:
        url = URL.create(
            "postgresql+psycopg",
            username=locator.user,
            host=locator.host,
            port=locator.port,
            database=locator.database,
        )
        engine = create_engine(
            url,
            connect_args={"connect_timeout": 2},
            pool_pre_ping=False,
        )
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"
                ),
                {"schema": locator.schema_name},
            ).scalars().all()
            tables = {str(row) for row in rows}
            if not tables:
                return SetupDatabaseData(status="empty", table_count=0)

            business_tables = tuple(sorted(tables & _BUSINESS_TABLES))
            if not business_tables:
                return SetupDatabaseData(
                    status="needs_confirmation",
                    table_count=len(tables),
                    code="UNRECOGNIZED_SCHEMA",
                    detail="数据库中有表，但不是当前个人学习结构；需要确认后才能使用。",
                )

            for name in _ROW_EVIDENCE_TABLES:
                if name not in business_tables:
                    continue
                try:
                    present = connection.execute(
                        text(f'SELECT 1 FROM "{name}" LIMIT 1')
                    ).first()
                except SQLAlchemyError:
                    return SetupDatabaseData(
                        status="needs_confirmation",
                        table_count=len(tables),
                        business_tables=business_tables,
                        code="ROW_EVIDENCE_UNAVAILABLE",
                        detail="无法只读检查关键表内容；为避免覆盖，暂不视为全新空间。",
                    )
                if present is not None:
                    return SetupDatabaseData(
                        status="existing",
                        table_count=len(tables),
                        business_tables=business_tables,
                        code="EXISTING_ROWS",
                        detail="检测到已有学习数据。",
                    )

            return SetupDatabaseData(
                status="needs_confirmation",
                table_count=len(tables),
                business_tables=business_tables,
                code="SCHEMA_WITHOUT_ROWS",
                detail="检测到学习数据表，但未发现关键行；需要确认后才能使用。",
            )
    except (SQLAlchemyError, OSError, ValueError, TypeError):
        return SetupDatabaseData(
            status="unavailable",
            code="DATABASE_UNREACHABLE",
            detail="无法只读连接数据库；请检查数据库服务、地址和账号。",
        )
    finally:
        if engine is not None:
            engine.dispose()


def probe_storage_data(root: Path | None) -> SetupStorageData:
    """Inspect only top-level entries; never create or modify the storage root."""

    if root is None:
        return SetupStorageData(
            status="unavailable",
            code="STORAGE_NOT_CONFIGURED",
            detail="尚未检测到可用的资料目录。",
        )
    path = Path(root)
    try:
        if not path.exists():
            return SetupStorageData(status="empty", entry_count=0)
        if not path.is_dir():
            return SetupStorageData(
                status="unavailable",
                code="STORAGE_NOT_DIRECTORY",
                detail="资料目录不是文件夹。",
            )
        count = 0
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith(".readiness-"):
                    continue
                count += 1
                if count >= 100:
                    break
        return SetupStorageData(
            status="existing" if count else "empty",
            entry_count=count,
        )
    except OSError:
        return SetupStorageData(
            status="unavailable",
            code="STORAGE_UNREADABLE",
            detail="资料目录无法读取；请检查权限。",
        )


def detect_setup_data_state(
    *,
    recommendation: SetupRecommendation,
    checked_at: datetime | None = None,
    database_probe: Callable[[DatabaseLocator | None], SetupDatabaseData] | None = None,
    storage_probe: Callable[[Path | None], SetupStorageData] | None = None,
) -> SetupDataState:
    """Combine read-only probes; only a fully empty target is `new`."""

    database = (database_probe or probe_database_data)(
        recommendation.business.database_locator
    )
    storage = (storage_probe or probe_storage_data)(
        recommendation.business.storage_root
    )

    status: Literal["unknown", "new", "existing", "needs_confirmation"]
    if database.status == "unavailable" or storage.status == "unavailable":
        status = "unknown"
        reason = (
            database.code if database.status == "unavailable" else storage.code
        )
        can_initialize = False
    elif database.status == "empty" and storage.status == "empty":
        status = "new"
        reason = None
        can_initialize = True
    elif database.status == "existing" and storage.status == "existing":
        status = "existing"
        reason = "EXISTING_DATA_DETECTED"
        can_initialize = False
    elif database.status == "existing" or storage.status == "existing":
        status = "needs_confirmation"
        reason = "PARTIAL_DATA_DETECTED"
        can_initialize = False
    elif database.status == "needs_confirmation" or storage.status == "existing":
        status = "needs_confirmation"
        reason = database.code or "EXISTING_DATA_NEEDS_CONFIRMATION"
        can_initialize = False
    else:
        status = "needs_confirmation"
        reason = database.code or "TARGET_NOT_VERIFIED"
        can_initialize = False

    return SetupDataState(
        status=status,
        can_initialize=can_initialize,
        reason=reason,
        database=database,
        storage=storage,
        checked_at=checked_at or datetime.now(UTC),
    )
