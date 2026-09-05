"""Redis 连接、就绪检查与生命周期辅助工具。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from redis import Redis
from redis.exceptions import RedisError

from backend.app.core.config import AppSettings, get_settings


class RedisNotReadyError(RuntimeError):
    """当 Redis 无法响应健康检查时抛出的异常。"""


def create_redis_client(
    settings: AppSettings | None = None,
    *,
    socket_connect_timeout: float = 5.0,
    socket_timeout: float = 5.0,
    health_check_interval: int = 30,
    decode_responses: bool = True,
) -> Redis:
    """根据运行配置创建 Redis 客户端，但不主动发起网络连接。"""

    runtime_settings = settings or get_settings()
    return Redis.from_url(
        str(runtime_settings.redis_url),
        encoding="utf-8",
        decode_responses=decode_responses,
        health_check_interval=max(0, health_check_interval),
        socket_connect_timeout=max(0.1, socket_connect_timeout),
        socket_timeout=max(0.1, socket_timeout),
    )


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    """返回进程级缓存的 Redis 客户端。"""

    return create_redis_client()


def check_redis_ready(client: Redis | None = None) -> bool:
    """通过 PING 检查 Redis 是否可用，并返回安全的本地开发提示。"""

    active_client = client or get_redis_client()
    try:
        active_client.ping()
    except (RedisError, OSError, TimeoutError) as exc:
        raise RedisNotReadyError(
            "Redis 未就绪，请确认本地 Redis 服务已启动，并检查 REDIS_URL 配置。"
        ) from exc
    return True


def close_redis_client(client: Redis | None = None) -> None:
    """关闭 Redis 客户端；未创建缓存客户端时安全地跳过关闭。"""

    using_cached_client = client is None
    if using_cached_client and get_redis_client.cache_info().currsize == 0:
        return

    active_client = client or get_redis_client()
    try:
        active_client.close()
    finally:
        if using_cached_client:
            get_redis_client.cache_clear()


@contextmanager
def redis_lifespan(client: Redis | None = None) -> Iterator[Redis]:
    """提供 Redis 使用上下文，并在退出时释放连接资源。"""

    active_client = client or get_redis_client()
    try:
        yield active_client
    finally:
        close_redis_client(active_client)
        if client is None:
            get_redis_client.cache_clear()


def reset_redis_cache() -> None:
    """关闭并清空缓存客户端，仅供测试或进程重启使用。"""

    if get_redis_client.cache_info().currsize == 0:
        return
    close_redis_client()


__all__ = [
    "RedisNotReadyError",
    "check_redis_ready",
    "close_redis_client",
    "create_redis_client",
    "get_redis_client",
    "redis_lifespan",
    "reset_redis_cache",
]
