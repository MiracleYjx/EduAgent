"""设置驱动的 Alembic 初始化：内存传 URL，先核验目标身份与权限。

不写进程环境、不把连接串放进 argv/日志。迁移仍使用项目既有 ``migrations/``
与 ``alembic.ini``；``migrations/env.py`` 在存在程序化 connection 时直接复用它。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError


class InitializationError(RuntimeError):
    """初始化前置核验失败；只携带稳定安全码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def require_test_identity(environment: str, test_instance_id: str | None) -> None:
    """Test 环境没有可核验的实例身份时，禁止进入真实初始化。"""

    if str(environment) == "test" and not test_instance_id:
        raise InitializationError("TARGET_IDENTITY_UNVERIFIED")


def verify_database_target(
    connection: Connection,
    *,
    expected: Mapping[str, Any],
    test_instance_id: str | None = None,
) -> None:
    """核验数据库身份、DDL 权限、pgvector 可用性；任何未知都失败关闭。"""

    try:
        row = connection.execute(
            text(
                "SELECT current_database(), current_user, current_schema()"
            )
        ).one()
    except SQLAlchemyError:
        raise InitializationError("TARGET_IDENTITY_UNVERIFIED") from None
    database, user, schema = (str(value) for value in row)
    if (
        database != str(expected["database"])
        or user != str(expected["user"])
        or schema != str(expected["schema"])
    ):
        raise InitializationError("TARGET_IDENTITY_MISMATCH")
    if test_instance_id is not None:
        try:
            actual = connection.execute(
                text("SELECT system_identifier::text FROM pg_control_system()")
            ).scalar_one()
        except SQLAlchemyError:
            raise InitializationError("TARGET_IDENTITY_UNVERIFIED") from None
        if str(actual) != str(test_instance_id):
            raise InitializationError("TARGET_IDENTITY_MISMATCH")
    try:
        privileges = connection.execute(
            text(
                "SELECT "
                "has_database_privilege(current_user, current_database(), 'CONNECT'), "
                "has_schema_privilege(current_user, current_schema(), 'USAGE'), "
                "has_schema_privilege(current_user, current_schema(), 'CREATE')"
            )
        ).one()
    except SQLAlchemyError:
        raise InitializationError("TARGET_PRIVILEGE_UNVERIFIED") from None
    if not all(bool(value) for value in privileges):
        raise InitializationError("TARGET_PRIVILEGE_MISSING")
    try:
        vector = connection.execute(
            text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        ).first()
    except SQLAlchemyError:
        raise InitializationError("PGVECTOR_UNAVAILABLE") from None
    if vector is None:
        raise InitializationError("PGVECTOR_UNAVAILABLE")


def verify_database_url(
    database_url: str,
    *,
    expected: Mapping[str, Any],
    test_instance_id: str | None = None,
) -> None:
    """打开一次短连接完成核验；连接串只存在于本进程内存。"""

    engine = create_engine(database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            verify_database_target(
                connection, expected=expected, test_instance_id=test_instance_id
            )
    except InitializationError:
        raise
    except (SQLAlchemyError, ValueError):
        raise InitializationError("TARGET_IDENTITY_UNVERIFIED") from None
    finally:
        engine.dispose()


def run_alembic_upgrade(
    database_url: str, *, project_root: Path | None = None
) -> None:
    """用程序化 connection 执行 ``alembic upgrade head``。"""

    from alembic import command
    from alembic.config import Config

    root = project_root or _project_root()
    engine = create_engine(database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            config = Config(str(root / "alembic.ini"))
            config.set_main_option("script_location", str(root / "migrations"))
            config.set_main_option("prepend_sys_path", str(root))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


__all__ = [
    "InitializationError",
    "require_test_identity",
    "run_alembic_upgrade",
    "verify_database_target",
    "verify_database_url",
]
