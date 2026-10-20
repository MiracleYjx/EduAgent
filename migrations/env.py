"""Alembic 环境，加载 EduAgent 全部 SQLAlchemy 模型。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.app import models
from backend.app.core.database import Base, create_database_engine

config = context.config
models.register_models()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """在不建立实时连接时执行迁移。"""

    engine = create_database_engine()
    try:
        context.configure(
            url=str(engine.url),
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        engine.dispose()


def configure_target(connection: Connection) -> None:
    # The fallback search_path resolves pgvector types, never another schema's
    # Alembic version table. Preserve any transaction owned by the caller.
    already_in_transaction = connection.in_transaction()
    schema = connection.scalar(text("SELECT current_schema()"))
    if not schema:
        raise RuntimeError("MIGRATION_TARGET_SCHEMA_MISSING")
    if not already_in_transaction:
        connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table_schema=str(schema),
    )


def run_migrations_online() -> None:
    """连接 PostgreSQL 执行迁移。

    设置驱动的初始化会通过 ``config.attributes["connection"]`` 传入短连接；
    该连接只存在于本进程内存，不写环境变量、argv 或日志。CLI 路径仍回退到
    既有的进程环境/`.env` 入口。
    """

    provided = config.attributes.get("connection")
    if provided is not None:
        configure_target(provided)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if url:
        from sqlalchemy import create_engine

        engine = create_engine(url, connect_args={"connect_timeout": 5})
    else:
        engine = create_database_engine()
    try:
        with engine.connect() as connection:
            configure_target(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
