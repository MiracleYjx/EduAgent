from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from backend.app.core.config import AppSettings
from backend.app.core.database import (
    DatabaseNotReadyError,
    check_postgres_ready,
    create_database_engine,
    create_session_factory,
    initialize_pgvector_extension,
)
from tests.unit.settings_helpers import build_test_settings


def build_settings() -> AppSettings:
    return build_test_settings()


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

    with pytest.raises(DatabaseNotReadyError, match="PostgreSQL 未就绪"):
        check_postgres_ready(engine)


def test_initialize_pgvector_extension() -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value

    initialize_pgvector_extension(engine)

    connection.execute.assert_called_once()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in str(
        connection.execute.call_args.args[0]
    )
