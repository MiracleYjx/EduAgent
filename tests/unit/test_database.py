from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from backend.app.core.config import AppSettings
from backend.app.core.database import (
    DatabaseNotReadyError,
    check_postgres_ready,
    create_database_engine,
    create_session_factory,
    initialize_pgvector_extension,
)


def build_settings() -> AppSettings:
    return AppSettings(
        database_url="postgresql+psycopg://user:password@localhost:5432/eduagent",
        redis_url="redis://localhost:6379/0",
        llm_provider="deepseek",
        deepseek_api_key=SecretStr("test-key"),
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        embedding_provider="local",
        rerank_provider="none",
        confidence_threshold=0.8,
    )


def test_create_database_engine_uses_configured_postgres_url() -> None:
    engine = create_database_engine(build_settings())

    assert str(engine.url) == "postgresql+psycopg://user:***@localhost:5432/eduagent"
    assert engine.pool._pre_ping is True
    engine.dispose()


def test_create_session_factory_disables_expiration() -> None:
    engine = MagicMock()

    session_factory = create_session_factory(engine)

    assert session_factory.kw["autoflush"] is False
    assert session_factory.kw["expire_on_commit"] is False


def test_check_postgres_ready_executes_probe_query() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value

    assert check_postgres_ready(engine) is True

    connection.execute.assert_called_once()
    assert "SELECT 1" in str(connection.execute.call_args.args[0])


def test_check_postgres_ready_raises_safe_error() -> None:
    engine = MagicMock()
    engine.connect.side_effect = OperationalError(
        "connect", {}, RuntimeError("offline")
    )

    with pytest.raises(DatabaseNotReadyError, match="PostgreSQL is not ready"):
        check_postgres_ready(engine)


def test_initialize_pgvector_extension() -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value

    initialize_pgvector_extension(engine)

    connection.execute.assert_called_once()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in str(
        connection.execute.call_args.args[0]
    )
