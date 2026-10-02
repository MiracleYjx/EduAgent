"""T157/T158 actual database pipeline/locking evidence; no quality claims. TCR §13."""

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from backend.app.ai.ingestion.ocr.base import BaseOCRProvider
from backend.app.ai.ingestion.ocr.schemas import OCRProviderInfo, OCRResult
from backend.app.ai.paper_extraction import PaperExtractor
from backend.app.core.database import Base
from backend.app.domain.enums import (
    ExtractedQuestionStatus,
    PaperImportStatus,
    UserRole,
)
from backend.app.models import Course, Role, User
from backend.app.services.paper_import_service import (
    PaperImportRunner,
    PaperImportService,
)
from tests.integration.test_question_source_migration import _empty_isolated_schema
from tests.unit.ingestion.test_paper_extraction import Provider, question
from tests.unit.settings_helpers import build_test_settings

SAMPLES = (
    Path(__file__).resolve().parents[2] / "benchmark/corpus/v2-draft-20261001/inputs"
)


@pytest.fixture
def context(tmp_path):
    with _empty_isolated_schema() as (engine, _schema):
        Base.metadata.create_all(engine, checkfirst=False)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        settings = build_test_settings(STORAGE_ROOT=tmp_path / "stored")
        with factory() as session:
            teacher = User(
                username="pipeline",
                email="pipeline@test.invalid",
                password_hash="unused",
                roles=[Role(name=UserRole.TEACHER)],
            )
            course = Course(name="pipeline", creator=teacher)
            session.add(course)
            session.commit()
            actor, course_id = teacher.id, course.id
        yield factory, settings, actor, course_id


def receive(context, name="paper_text.pdf"):
    factory, settings, actor, course = context
    with factory() as session:
        return PaperImportService(session, root=settings.storage_root).upload(
            course, filename=name, content=(SAMPLES / name).read_bytes(), actor_id=actor
        )


def detail(context, identity):
    factory, settings, actor, _ = context
    with factory() as session:
        return PaperImportService(session, root=settings.storage_root).get(
            identity, actor_id=actor
        )


def test_mixed_selects_only_actual_scan_page_and_zero_question_fails(context):
    factory, settings, _actor, _ = context
    calls = []

    class OCR(BaseOCRProvider):
        def describe(self):
            return OCRProviderInfo(provider="test", model=None, ready=True, reason=None)

        async def extract_text(self, path):
            from PIL import Image

            with Image.open(path) as img:
                assert img.width > 0 and img.height > 0
            calls.append(path)
            return OCRResult(text="2. 原始扫描页 OCR 字符", confidence=None, regions=[])

    received = receive(context, "paper_mixed.pdf")
    PaperImportRunner(
        factory,
        settings=settings,
        ocr=OCR(),
        extractor=PaperExtractor(Provider([{"questions": [question([1, 2])]}])),
    ).run(received.id)
    result = detail(context, received.id)
    assert result.status == PaperImportStatus.PENDING_REVIEW and len(calls) == 1
    assert (
        result.pages[0].ocr_text is None
        and result.pages[1].ocr_text == "2. 原始扫描页 OCR 字符"
    )
    assert result.questions[0].source_page_ids == [p.id for p in result.pages]
    empty = receive(context)
    PaperImportRunner(
        factory,
        settings=settings,
        extractor=PaperExtractor(Provider([{"questions": []}])),
    ).run(empty.id)
    failed = detail(context, empty.id)
    assert (
        failed.status == PaperImportStatus.FAILED
        and failed.error_code == "PAPER_EXTRACTION_FAILED"
    )
    assert failed.parsed_page_count == 1 and failed.question_count == 0


def test_failed_later_batch_preserves_pages_and_previous_extraction(context):
    factory, settings, _actor, _ = context
    received = receive(context, "paper_cross_page.pdf")

    class Failing(Provider):
        async def generate_structured(self, messages, schema, model=None):
            if len(self.inputs):
                raise TimeoutError("simulated transport")
            return await super().generate_structured(messages, schema, model)

    PaperImportRunner(
        factory,
        settings=settings,
        extractor=PaperExtractor(
            Failing([{"questions": [question([1])]}]), batch_size=1
        ),
    ).run(received.id)
    result = detail(context, received.id)
    assert (
        result.status == PaperImportStatus.FAILED
        and result.parsed_page_count == 2
        and result.question_count == 1
    )
    assert "TimeoutError" in result.error_message
    assert result.questions[0].status == ExtractedQuestionStatus.PENDING_CORRECTION
