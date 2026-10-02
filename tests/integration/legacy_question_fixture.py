"""Historical question inserts use the actual predecessor table, not today's mapper."""
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import MetaData, Table
from sqlalchemy.engine import Connection


def insert_legacy_question(connection: Connection, **values):
    questions = Table("questions", MetaData(), autoload_with=connection)
    identity = uuid4()
    now = datetime.now(UTC)
    connection.execute(questions.insert().values(
        id=identity, created_at=now, updated_at=now, status="Draft", knowledge_points=[],
        **values,
    ))
    return identity
