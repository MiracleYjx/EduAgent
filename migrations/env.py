"""Alembic 环境，加载 EduAgent 全部 SQLAlchemy 模型。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

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


def run_migrations_online() -> None:
    """连接 PostgreSQL 执行迁移。"""

    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
