"""T160 actual PostgreSQL locking and OCR fault acceptance; TCR section 18.

The extraction response and OCR fault are explicit test inputs, not quality evidence.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from threading import Event
from time import monotonic

import pytest
from PIL import Image
from sqlalchemy import func, select, text

from backend.app.ai.ingestion.ocr.base import BaseOCRProvider, OCRProviderError
from backend.app.ai.ingestion.ocr.schemas import OCRProviderInfo
from backend.app.ai.paper_extraction import PaperExtractor
from backend.app.domain.enums import ExtractedQuestionStatus, PaperImportStatus
from backend.app.models import ExtractedQuestion, Question
from backend.app.schemas.paper_import import CorrectionPayload
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.paper_import_service import PaperImportRunner
from backend.app.services.question_correction_service import QuestionCorrectionService
from tests.integration.test_paper_import_flow import (
    SAMPLES,
    detail,
    receive,
)
from tests.integration.test_paper_import_flow import (
    context as _context,
)
from tests.unit.ingestion.test_paper_extraction import Provider, question

context = _context


def pending_question(context):
    factory, settings, actor, _course = context
    # Match production create_session_factory rather than the fixture's default.
    factory.configure(autoflush=False)
    paper = receive(context)
    PaperImportRunner(
        factory,
        settings=settings,
        extractor=PaperExtractor(Provider([{"questions": [question([1])]}])),
    ).run(paper.id)
    loaded = detail(context, paper.id)
    assert loaded.status == PaperImportStatus.PENDING_REVIEW
    question_id = loaded.questions[0].id
    with factory() as session:
        QuestionCorrectionService(session, root=settings.storage_root).patch(
            paper.id,
            question_id,
            CorrectionPayload(score="2", assets=[]),
            actor_id=actor,
        )
    return paper.id, question_id


def test_patch_lock_blocks_commit_until_corrected_values_are_committed(context):
    factory, settings, actor, _course = context
    paper_id, question_id = pending_question(context)
    patch_held, commit_started, release_patch = Event(), Event(), Event()
    pids = {}

    class PausedPatch(QuestionCorrectionService):
        def _commit(self):
            # Pause at the transaction boundary; the real service acquired its locks.
            pids["patch"] = self.session.scalar(text("SELECT pg_backend_pid()"))
            patch_held.set()
            if not release_patch.wait(timeout=15):
                raise TimeoutError("test did not release the correction transaction")
            super()._commit()

    def patch():
        with factory() as session:
            return PausedPatch(session, root=settings.storage_root).patch(
                paper_id,
                question_id,
                CorrectionPayload(content="Teacher corrected input", score="7.25"),
                actor_id=actor,
            )

    def commit():
        with factory() as session:
            pids["commit"] = session.scalar(text("SELECT pg_backend_pid()"))
            commit_started.set()
            return QuestionCorrectionService(
                session, root=settings.storage_root
            ).commit(paper_id, [question_id], actor_id=actor)

    with ThreadPoolExecutor(max_workers=2) as workers:
        patch_job = workers.submit(patch)
        commit_job = None
        try:
            assert patch_held.wait(
                timeout=10
            ), "PATCH never reached its commit boundary"
            commit_job = workers.submit(commit)
            assert commit_started.wait(timeout=10), "commit worker did not start"
            deadline = monotonic() + 5
            blocked = False
            with factory() as observer:
                while monotonic() < deadline:
                    blocked = observer.scalar(
                        text("SELECT :blocker = ANY(pg_blocking_pids(:waiter))"),
                        {"blocker": pids["patch"], "waiter": pids["commit"]},
                    )
                    if blocked:
                        break
                    release_patch.wait(timeout=0.02)
            assert blocked, "PostgreSQL did not observe commit waiting for PATCH"
            assert (
                not commit_job.done()
            ), "confirmation completed before PATCH committed"
        finally:
            release_patch.set()
        patched = patch_job.result(timeout=20)
        assert commit_job is not None
        confirmed = commit_job.result(timeout=20)

    assert patched.content == "Teacher corrected input"
    formal_id = confirmed.questions[0].question_id
    with factory() as session:
        formal = session.get(Question, formal_id)
        extracted = session.get(ExtractedQuestion, question_id)
        assert formal.content == "Teacher corrected input"
        assert formal.score == Decimal("7.25")
        assert extracted.content == formal.content
        assert extracted.score == formal.score
        assert extracted.question_id == formal.id
        assert extracted.status == ExtractedQuestionStatus.CORRECTED
        assert session.scalar(select(func.count()).select_from(Question)) == 1
    reloaded = detail(context, paper_id)
    assert reloaded.status == PaperImportStatus.READY
    assert reloaded.questions[0].source_page_ids == [page.id for page in reloaded.pages]


def test_confirmation_first_rejects_late_correction_from_old_session(context):
    factory, settings, actor, _course = context
    paper_id, question_id = pending_question(context)
    with factory() as stale:
        before = stale.get(ExtractedQuestion, question_id)
        assert before.status == ExtractedQuestionStatus.PENDING_CORRECTION
        original_content, original_score = before.content, before.score
        with factory() as writer:
            confirmed = QuestionCorrectionService(
                writer, root=settings.storage_root
            ).commit(paper_id, [question_id], actor_id=actor)
        with pytest.raises(FileStorageError) as failure:
            QuestionCorrectionService(stale, root=settings.storage_root).patch(
                paper_id,
                question_id,
                CorrectionPayload(content="Late stale overwrite", score="99"),
                actor_id=actor,
            )
        assert failure.value.code == "PAPER_STATE_CONFLICT"
        assert failure.value.http_status == 409

    with factory() as session:
        source = session.get(ExtractedQuestion, question_id)
        formal = session.get(Question, confirmed.questions[0].question_id)
        assert source.status == ExtractedQuestionStatus.CORRECTED
        assert source.content == formal.content == original_content
        assert source.score == formal.score == original_score
        assert source.question_id == formal.id
        assert session.scalar(select(func.count()).select_from(Question)) == 1
    assert detail(context, paper_id).status == PaperImportStatus.READY


def test_ready_ocr_call_failure_preserves_original_and_rendered_page(context):
    factory, settings, actor, _course = context
    factory.configure(autoflush=False)
    paper = receive(context, "paper_scan.pdf")
    calls = []
    extraction = Provider([])

    class FailedOCR(BaseOCRProvider):
        def describe(self):
            return OCRProviderInfo(
                provider="t160_fault_injection",
                model=None,
                ready=True,
                reason=None,
            )

        async def extract_text(self, image_path: Path):
            with Image.open(image_path) as image:
                assert image.format == "PNG" and image.width > 0 and image.height > 0
            calls.append(image_path)
            raise OCRProviderError(
                "OCR_CALL_FAILED", "T160 injected OCR transport failure"
            ) from RuntimeError("explicit test transport failure")

    PaperImportRunner(
        factory,
        settings=settings,
        ocr=FailedOCR(),
        extractor=PaperExtractor(extraction),
    ).run(paper.id)
    reloaded = detail(context, paper.id)
    assert reloaded.status == PaperImportStatus.FAILED
    assert reloaded.error_code == "PAPER_OCR_FAILED"
    assert "OCR_CALL_FAILED" in reloaded.error_message
    assert "OCR" in reloaded.error_message
    assert reloaded.parsed_page_count == 1
    assert reloaded.question_count == 0 and reloaded.questions == []
    assert len(calls) == 1 and extraction.inputs == []
    page = reloaded.pages[0]
    assert page.ocr_text is None and page.ocr_confidence is None
    with factory() as session:
        files = FileStorageService(session, root=settings.storage_root)
        original, _ = files.download(reloaded.original_file_id, actor_id=actor)
        assert original.read_bytes() == (SAMPLES / "paper_scan.pdf").read_bytes()
        saved_page, _ = files.download(page.file_id, actor_id=actor)
        assert saved_page == calls[0]
        with Image.open(BytesIO(saved_page.read_bytes())) as image:
            assert image.format == "PNG"
            assert image.size == (page.width, page.height)
