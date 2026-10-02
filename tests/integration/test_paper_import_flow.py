"""T157/T158 actual database pipeline/locking evidence; no quality claims. TCR §13."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import func, select
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
from backend.app.models import Course, ExtractedQuestion, Question, Role, User
from backend.app.schemas.paper_import import CorrectionPayload
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.paper_import_service import (
    PaperImportRunner,
    PaperImportService,
)
from backend.app.services.question_correction_service import QuestionCorrectionService
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


def test_concurrent_duplicate_commit_and_stale_session_edit_are_serialized(context):
    factory, settings, actor, _ = context
    received = receive(context)
    PaperImportRunner(
        factory,
        settings=settings,
        extractor=PaperExtractor(Provider([{"questions": [question([1])]}])),
    ).run(received.id)
    qid = detail(context, received.id).questions[0].id
    with factory() as session:
        QuestionCorrectionService(session, root=settings.storage_root).patch(
            received.id, qid, CorrectionPayload(score="2", assets=[]), actor_id=actor
        )
    barrier = threading.Barrier(2)

    def confirm():
        with factory() as session:
            barrier.wait(timeout=10)
            result = QuestionCorrectionService(
                session, root=settings.storage_root
            ).commit(received.id, [qid], actor_id=actor)
            return result.questions[0].question_id

    # Populate an ORM identity map before the other sessions commit.
    with factory() as stale:
        source = stale.get(ExtractedQuestion, qid)
        assert source.status == ExtractedQuestionStatus.PENDING_CORRECTION
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(confirm) for _ in range(2)]
            identities = [job.result(timeout=20) for job in jobs]
        assert identities[0] == identities[1]
        with pytest.raises(FileStorageError) as exc:
            QuestionCorrectionService(stale, root=settings.storage_root).patch(
                received.id, qid, CorrectionPayload(content="迟到修改"), actor_id=actor
            )
        assert exc.value.code == "PAPER_STATE_CONFLICT"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Question)) == 1
        assert session.get(ExtractedQuestion, qid).content == "解释跨页问题"


def test_ordered_options_roundtrip_reorder_and_formal_update(context):
    from backend.app.services.question_service import QuestionService

    factory, settings, actor, _ = context
    options = {"C": "7", "A": "5", "D": "8", "B": "6"}
    reordered = {"B": "6", "D": "8", "A": "5", "C": "7"}
    received = receive(context)
    PaperImportRunner(
        factory,
        settings=settings,
        extractor=PaperExtractor(
            Provider(
                [
                    {
                        "questions": [
                            question(
                                [1],
                                question_type="SINGLE_CHOICE",
                                options=options,
                                score="2",
                            ),
                        ]
                    }
                ]
            )
        ),
    ).run(received.id)
    first = detail(context, received.id).questions[0]
    assert list(first.options) == list(options) and first.order_preserved is True
    with factory() as session:
        source = session.get(ExtractedQuestion, first.id)
        source.image_assessment = {"context_revision": 0}
        session.commit()
        result = QuestionCorrectionService(session, root=settings.storage_root).patch(
            received.id,
            first.id,
            CorrectionPayload(options=reordered, assets=[]),
            actor_id=actor,
        )
        assert result.image_assessment["context_revision"] == 1
    reloaded = detail(context, received.id).questions[0]
    assert list(reloaded.options) == list(reordered)
    with factory() as session:
        result = QuestionCorrectionService(session, root=settings.storage_root).commit(
            received.id,
            [first.id],
            actor_id=actor,
        )
        formal_id = result.questions[0].question_id
    with factory() as session:
        formal = session.get(Question, formal_id)
        assert (
            list(formal.options) == list(reordered) and formal.order_preserved is True
        )
        formal.image_assessment = {"context_revision": 0}
        formal.order_preserved = False
        session.commit()
        unchanged = QuestionService(session).update_question(
            formal_id,
            options=reordered,
            teacher_id=actor,
            analysis="只改解析",
        )
        assert unchanged.order_preserved is False
        revision = session.get(Question, formal_id).image_assessment["context_revision"]
        changed = QuestionService(session).update_question(
            formal_id, options=options, teacher_id=actor
        )
        assert changed.order_preserved is True and list(changed.options) == list(
            options
        )
        assert (
            session.get(Question, formal_id).image_assessment["context_revision"]
            == revision + 1
        )
    with factory() as session:
        assert list(session.get(Question, formal_id).options) == list(options)
        assert session.get(ExtractedQuestion, first.id).options == reordered
