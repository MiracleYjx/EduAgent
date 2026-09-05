"""PostgreSQL 引擎、会话、就绪检查与扩展辅助工具。"""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.core.config import AppSettings, get_settings


class DatabaseNotReadyError(RuntimeError):
    """当 PostgreSQL 无法响应健康检查或初始化查询时抛出的异常。"""


class Base(DeclarativeBase):
    """供 Alembic 和后续 SQLAlchemy 模型使用的基础元数据。"""


def create_database_engine(
    settings: AppSettings | None = None,
    *,
    connect_timeout: int = 5,
    echo: bool = False,
) -> Engine:
    """
    创建 PostgreSQL 引擎（不主动打开网络连接）。

    参数：
        settings: 应用配置对象，若为空则自动加载
        connect_timeout: 连接超时秒数
        echo: 是否输出 SQL 日志

    返回：
        已配置的 SQLAlchemy 引擎实例
    """

    runtime_settings = settings or get_settings()
    return create_engine(
        str(runtime_settings.database_url),
        connect_args={"connect_timeout": max(1, connect_timeout)},
        echo=echo,
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """返回进程级懒加载数据库引擎（单例缓存）。"""

    return create_database_engine()


def create_session_factory(
    engine: Engine | None = None,
) -> sessionmaker[Session]:
    """
    创建配置好的 SQLAlchemy 会话工厂。

    参数：
        engine: 数据库引擎，若为空则使用全局引擎

    返回：
        会话工厂对象
    """

    return sessionmaker(
        bind=engine or get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """返回进程级会话工厂（单例缓存）。"""

    return create_session_factory()


def get_db() -> Generator[Session, None, None]:
    """
    生成请求作用域的数据库会话，异常时自动回滚。

    用法：
        with next(get_db()) as session:
            ...

    或 FastAPI 依赖注入：
        def route(db: Session = Depends(get_db)):
            ...
    """

    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_postgres_ready(engine: Engine | None = None) -> bool:
    """
    检查 PostgreSQL 是否能够响应最基本的探测查询。

    参数：
        engine: 数据库引擎，若为空则使用全局引擎

    返回：
        就绪返回 True

    异常：
        DatabaseNotReadyError: 数据库无法连接或响应
    """

    active_engine = engine or get_engine()
    try:
        with active_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise DatabaseNotReadyError("PostgreSQL 未就绪") from exc
    return True


def initialize_pgvector_extension(engine: Engine | None = None) -> None:
    """
    确保 PostgreSQL 的 pgvector 扩展已安装。

    首次调用时会创建 `vector` 扩展，后续调用自动跳过。

    参数：
        engine: 数据库引擎，若为空则使用全局引擎

    异常：
        DatabaseNotReadyError: 扩展安装失败
    """

    active_engine = engine or get_engine()
    try:
        with active_engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except SQLAlchemyError as exc:
        raise DatabaseNotReadyError("无法初始化 PostgreSQL pgvector 扩展") from exc


def reset_database_caches() -> None:
    """清空缓存的引擎与会话工厂实例（仅用于测试环境）。"""

    get_session_factory.cache_clear()
    engine = get_engine()
    get_engine.cache_clear()
    engine.dispose()


__all__ = [
    "Base",
    "DatabaseNotReadyError",
    "check_postgres_ready",
    "create_database_engine",
    "create_session_factory",
    "get_db",
    "get_engine",
    "get_session_factory",
    "initialize_pgvector_extension",
    "reset_database_caches",
]