"""H02 测试所需的隔离 PostgreSQL 数据库空间。

TCR（2026-09-14）：Ready 必须同时具备真实向量与全文索引，原 SQLite 成功替身
不足以验证此合同；摄取成功用例使用临时 schema，退出时只删除本次生成的 schema。
缺少 PostgreSQL/pgvector 时明确 skip；恢复条件为启动既有 PostgreSQL 并安装 pgvector。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.core.config import get_settings
from backend.app.core.database import Base, create_database_engine


@contextmanager
def isolated_postgres_engine() -> Iterator[Engine]:
    """提供真实 PostgreSQL 引擎，测试提交不影响现有业务表。"""

    engine = create_database_engine(get_settings(), connect_timeout=3)
    schema = f"test_ready_{uuid4().hex}"
    created = False

    @event.listens_for(engine, "connect")
    def set_test_schema(connection, _record) -> None:
        """每条连接都优先访问本次测试的表和枚举类型。"""

        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", public')
        connection.commit()

    try:
        try:
            with engine.connect() as connection:
                installed = connection.scalar(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                )
        except SQLAlchemyError as exc:
            pytest.skip(f"PostgreSQL 不可用：{type(exc).__name__}；启动后重新运行。")
        if not installed:
            pytest.skip("缺少 pgvector 扩展；安装既有数据库扩展后重新运行。")
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        with engine.begin() as connection:
            Base.metadata.create_all(connection, checkfirst=False)
        yield engine
    finally:
        if created:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()
