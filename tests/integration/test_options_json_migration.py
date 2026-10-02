"""T158 real PostgreSQL JSON storage and honest legacy ordering. TCR §14."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, select, text

from backend.app.core import database
from tests.integration.test_question_source_migration import _empty_isolated_schema

ORDERED = {"C": "7", "A": "5", "D": "8", "B": "6"}


@pytest.mark.parametrize("formal_old_type", ["json", "jsonb"])
def test_legacy_options_conversion_and_new_order_roundtrip(
    monkeypatch, formal_old_type
):
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(config, "0017_extracted_order")
        user, course, document, imported, linked, standalone = [
            uuid4() for _ in range(6)
        ]
        now = datetime.now(UTC)

        def table(conn, name):
            return Table(name, MetaData(), autoload_with=conn, schema=schema)

        with engine.begin() as conn:
            if formal_old_type == "jsonb":
                # Controlled compatibility case; the canonical 0002 column is JSON.
                conn.execute(
                    text(
                        "ALTER TABLE questions ALTER COLUMN options TYPE jsonb USING options::jsonb"
                    )
                )
            conn.execute(
                table(conn, "users")
                .insert()
                .values(
                    id=user,
                    username="json_order",
                    email="json@test.invalid",
                    password_hash="unused",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                table(conn, "courses")
                .insert()
                .values(
                    id=course,
                    name="json",
                    created_by=user,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                table(conn, "documents")
                .insert()
                .values(
                    id=document,
                    course_id=course,
                    uploaded_by=user,
                    original_filename="paper.pdf",
                    file_format="pdf",
                    purpose="paper_source",
                    status="Ready",
                    retryable=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                table(conn, "paper_imports")
                .insert()
                .values(
                    id=imported,
                    course_id=course,
                    uploaded_by=user,
                    document_id=document,
                    original_filename="paper.pdf",
                )
            )
            formal = table(conn, "questions")
            for identity in [linked, standalone]:
                conn.execute(
                    formal.insert().values(
                        id=identity,
                        course_id=course,
                        type="SINGLE_CHOICE",
                        content="legacy",
                        options=ORDERED,
                        knowledge_points=[],
                        score=2,
                        status="Draft",
                        created_by=user,
                        created_at=now,
                        updated_at=now,
                    )
                )
            extracted = table(conn, "extracted_questions")
            cases = [
                (ORDERED, linked),
                (["C. 7", "A. 5"], None),
                ({}, None),
                (None, None),
            ]
            identities = []
            for options, question_id in cases:
                identity = uuid4()
                identities.append(identity)
                conn.execute(
                    extracted.insert().values(
                        id=identity,
                        paper_import_id=imported,
                        source_page_ids=[],
                        extracted_by="LLM",
                        options=options,
                        question_id=question_id,
                        status="Corrected" if question_id is not None else "Extracted",
                    )
                )
            old_order = list(
                conn.scalar(
                    select(extracted.c.options).where(extracted.c.id == identities[0])
                )
            )

        command.upgrade(config, "0018_options_json")
        with engine.connect() as conn:
            for name in ["questions", "extracted_questions"]:
                assert (
                    conn.scalar(
                        text(
                            "SELECT data_type FROM information_schema.columns "
                            "WHERE table_schema=:schema AND table_name=:table AND column_name='options'"
                        ),
                        {"schema": schema, "table": name},
                    )
                    == "json"
                )
            extracted = table(conn, "extracted_questions")
            formal = table(conn, "questions")
            rows = [
                conn.execute(
                    select(extracted.c.options, extracted.c.order_preserved).where(
                        extracted.c.id == identity
                    )
                ).one()
                for identity in identities
            ]
            assert (
                list(rows[0].options) == old_order and rows[0].order_preserved is False
            )
            assert [row.order_preserved for row in rows[1:]] == [True, True, True]
            assert (
                rows[1].options == ["C. 7", "A. 5"]
                and rows[2].options == {}
                and rows[3].options is None
            )
            assert (
                conn.scalar(
                    select(formal.c.order_preserved).where(formal.c.id == linked)
                )
                is False
            )
            assert conn.scalar(
                select(formal.c.order_preserved).where(formal.c.id == standalone)
            ) is (formal_old_type == "json")
            if formal_old_type == "json":
                assert list(
                    conn.scalar(
                        select(formal.c.options).where(formal.c.id == standalone)
                    )
                ) == list(ORDERED)

        new_id = uuid4()
        with engine.begin() as conn:
            conn.execute(
                extracted.insert().values(
                    id=new_id,
                    paper_import_id=imported,
                    source_page_ids=[],
                    extracted_by="LLM",
                    options=ORDERED,
                )
            )
        with engine.connect() as conn:
            row = conn.execute(
                select(extracted.c.options, extracted.c.order_preserved).where(
                    extracted.c.id == new_id
                )
            ).one()
            assert list(row.options) == list(ORDERED) and row.order_preserved is True
        with pytest.raises(RuntimeError, match="选项顺序"):
            command.downgrade(config, "0017_extracted_order")
        with engine.begin() as conn:
            conn.execute(extracted.update().values(options=None, order_preserved=True))
            conn.execute(formal.update().values(order_preserved=True))
        command.downgrade(config, "0017_extracted_order")
        with engine.connect() as conn:
            assert "order_preserved" not in table(conn, "extracted_questions").c
            assert "order_preserved" not in table(conn, "questions").c
        command.upgrade(config, "0018_options_json")
