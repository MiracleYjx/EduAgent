"""Alembic environment configured from EduAgent runtime settings."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from backend.app.core.database import Base, create_database_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without creating a live database connection."""

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
    """Run migrations against the configured PostgreSQL database."""

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
