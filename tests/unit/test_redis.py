from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr
from redis.exceptions import RedisError

from backend.app.core.config import AppSettings
from backend.app.core.redis import (
    RedisNotReadyError,
    check_redis_ready,
    close_redis_client,
    create_redis_client,
    redis_lifespan,
)


def build_settings() -> AppSettings:
    return AppSettings(
        database_url="postgresql+psycopg://user:password@localhost:5432/eduagent",
        redis_url="redis://:local-password@localhost:6379/0",
        llm_provider="deepseek",
        deepseek_api_key=SecretStr("test-key"),
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        embedding_provider="local",
        rerank_provider="none",
        confidence_threshold=0.8,
    )


def test_create_redis_client_uses_safe_connection_defaults() -> None:
    client = MagicMock()

    with patch(
        "backend.app.core.redis.Redis.from_url",
        return_value=client,
    ) as from_url:
        actual = create_redis_client(
            build_settings(),
            socket_connect_timeout=3,
            socket_timeout=7,
        )

    assert actual is client
    from_url.assert_called_once_with(
        "redis://:local-password@localhost:6379/0",
        encoding="utf-8",
        decode_responses=True,
        health_check_interval=30,
        socket_connect_timeout=3,
        socket_timeout=7,
    )


def test_check_redis_ready_pings_client() -> None:
    client = MagicMock()

    assert check_redis_ready(client) is True

    client.ping.assert_called_once_with()


def test_check_redis_ready_returns_safe_local_development_error() -> None:
    client = MagicMock()
    client.ping.side_effect = RedisError("connection failed")

    with pytest.raises(RedisNotReadyError, match="Redis 未就绪") as error:
        check_redis_ready(client)

    assert "local-password" not in str(error.value)
    assert "redis://" not in str(error.value)


def test_close_redis_client_closes_connection() -> None:
    client = MagicMock()

    close_redis_client(client)

    client.close.assert_called_once_with()


def test_redis_lifespan_closes_connection_after_use() -> None:
    client = MagicMock()

    with redis_lifespan(client) as active_client:
        assert active_client is client

    client.close.assert_called_once_with()
