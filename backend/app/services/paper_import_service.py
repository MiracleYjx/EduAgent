"""Paper source ingestion: durable stages, real progress, no knowledge indexing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.ingestion.ocr.base import BaseOCRProvider, OCRProviderError
from backend.app.ai.ingestion.ocr.factory import create_ocr_provider
from backend.app.ai.ingestion.paper_pipeline import PaperInputError, open_paper
from backend.app.ai.llm.base import BaseLLMProvider
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.ai.paper_extraction import PaperExtractor, TextPage
from backend.app.ai.paper_extraction.service import PaperExtractionError
from backend.app.core.config import AppSettings
from backend.app.core.retry_policy import ProviderCallError, ProviderExecutionError
from backend.app.domain.enums import (
    DocumentPurpose,
    DocumentStatus,
    ExtractedQuestionStatus,
    PaperImportStatus,
)
from backend.app.models import Document, ExtractedQuestion, PaperImport, SourcePage
from backend.app.schemas.paper_import import (
    ExtractedQuestionView,
    PaperImportView,
    SourcePageView,
)
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.question_asset_service import QuestionAssetService

logger = logging.getLogger(__name__)
ACTIVE = {
    PaperImportStatus.UPLOADED,
    PaperImportStatus.PARSING,
    PaperImportStatus.EXTRACTING,
}


def question_view(record: ExtractedQuestion) -> ExtractedQuestionView:
    fields = {
        name: getattr(record, name) for name in ExtractedQuestionView.model_fields
    }
    if fields["assets"] is not None:
        fields["assets"] = [
            {k: v for k, v in asset.items() if k != "file_meta"}
            for asset in fields["assets"]
        ]
    return ExtractedQuestionView.model_validate(fields)


class PaperImportService:
    def __init__(self, session: Session, *, root: Path | None = None):
        self.session = session
        self.files = FileStorageService(session, root=root)
        self.assets = QuestionAssetService(session, root=root)

    def get_record(
        self, identity: UUID, actor_id: UUID, *, lock: bool = False
    ) -> PaperImport:
        statement = (
            select(PaperImport)
            .where(PaperImport.id == identity)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        record = self.session.scalar(statement)
        if record is None:
            raise FileStorageError(
                "PAPER_NOT_FOUND", "导入记录不存在。", http_status=404
            )
        self.assets._teacher(record.course_id, actor_id)
        return record

    def questions(
        self, identity: UUID, *, actor_id: UUID
    ) -> list[ExtractedQuestionView]:
        self.get_record(identity, actor_id)
        records = self.session.scalars(
            select(ExtractedQuestion)
            .where(ExtractedQuestion.paper_import_id == identity)
            .order_by(
                ExtractedQuestion.order_index.asc().nulls_last(),
                ExtractedQuestion.created_at,
                ExtractedQuestion.id,
            )
            .execution_options(populate_existing=True)
        )
        return [question_view(record) for record in records]

    def get(self, identity: UUID, *, actor_id: UUID) -> PaperImportView:
        record = self.get_record(identity, actor_id)
        pages = list(
            self.session.scalars(
                select(SourcePage)
                .where(SourcePage.paper_import_id == identity)
                .order_by(SourcePage.page_number)
            )
        )
        questions = self.questions(identity, actor_id=actor_id)
        diagnostics = []
        for file_id in [
            "d_" + record.document_id.hex,
            *["p_" + page.id.hex for page in pages],
        ]:
            try:
                self.files.download(file_id, actor_id=actor_id)
            except FileStorageError as exc:
                diagnostics.append(
                    {"file_id": file_id, "code": exc.code, "message": str(exc)}
                )
        values = {
            name: getattr(record, name)
            for name in (
                "id",
                "course_id",
                "uploaded_by",
                "document_id",
                "original_filename",
                "page_count",
                "status",
                "error_code",
                "error_message",
                "created_at",
                "updated_at",
            )
        }
        return PaperImportView(
            **values,
            original_file_id="d_" + record.document_id.hex,
            pages=[
                SourcePageView(
                    id=p.id,
                    paper_import_id=p.paper_import_id,
                    page_number=p.page_number,
                    file_id="p_" + p.id.hex,
                    width=p.width,
                    height=p.height,
                    ocr_text=p.ocr_text,
                    ocr_confidence=p.ocr_confidence,
                )
                for p in pages
            ],
            questions=questions,
            parsed_page_count=len(pages),
            question_count=len(questions),
            file_diagnostics=diagnostics,
        )

    def list(self, course_id: UUID, *, actor_id: UUID) -> list[PaperImportView]:
        self.assets._teacher(course_id, actor_id)
        ids = list(
            self.session.scalars(
                select(PaperImport.id)
                .where(PaperImport.course_id == course_id)
                .order_by(PaperImport.created_at.desc(), PaperImport.id)
            )
        )
        return [self.get(identity, actor_id=actor_id) for identity in ids]

    def upload(
        self, course_id: UUID, *, filename: str, content: bytes, actor_id: UUID
    ) -> PaperImportView:
        self.assets._teacher(course_id, actor_id)
        with open_paper(content) as source:
            file_format = source.file_format
        filename = PureWindowsPath(filename).name.strip()
        if not filename or len(filename) > 255:
            raise FileStorageError(
                "PAPER_FILENAME_INVALID", "文件名须为 1–255 字。", http_status=422
            )
        document = Document(
            id=uuid4(),
            course_id=course_id,
            uploaded_by=actor_id,
            purpose=DocumentPurpose.PAPER_SOURCE,
            knowledge_base_id=None,
            original_filename=filename,
            file_format=file_format,
            status=DocumentStatus.READY,
        )
        stored = self.files.store_document(document, content, actor_id=actor_id)
        document.storage_path = stored.storage_path
        document.file_metadata = stored.metadata.model_dump(mode="json")
        record = PaperImport(
            id=uuid4(),
            course_id=course_id,
            uploaded_by=actor_id,
            document=document,
            original_filename=filename,
            status=PaperImportStatus.UPLOADED,
        )
        self.session.add(record)
        try:
            self.session.commit()
        except SQLAlchemyError as exc:
            self.session.rollback()
            self.files.fail_receipt(
                stored,
                code="PAPER_PERSISTENCE_FAILED",
                message="原卷登记失败，已保存文件保留。",
            )
            raise FileStorageError(
                "PAPER_PERSISTENCE_FAILED",
                "原卷登记失败，未受理导入。",
                http_status=503,
            ) from exc
        self.files.commit_receipt(stored, current_status="Ready")
        return self.get(record.id, actor_id=actor_id)


class PaperImportRunner:
    """Runs on the existing in-process background executor with its own sessions."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        settings: AppSettings,
        extractor: PaperExtractor | None = None,
        ocr: BaseOCRProvider | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.extractor = extractor
        self.ocr = ocr

    def _record(self, session: Session, identity: UUID) -> PaperImport:
        return session.scalars(
            select(PaperImport)
            .where(PaperImport.id == identity)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()

    def recover_interrupted(self) -> int:
        with self.session_factory() as session:
            records = list(
                session.scalars(
                    select(PaperImport)
                    .where(PaperImport.status.in_(ACTIVE))
                    .with_for_update()
                )
            )
            for record in records:
                previous = record.status.value
                record.status = PaperImportStatus.FAILED
                record.error_code = "PAPER_INTERRUPTED"
                record.error_message = (
                    "上次导入在 " + previous + " 阶段中断；已保存材料保留，请重新导入。"
                )
            session.commit()
            return len(records)

    def run(self, identity: UUID) -> None:
        asyncio.run(self._run(identity))

    async def _run(self, identity: UUID) -> None:
        owned_provider: BaseLLMProvider | None = None
        phase = "解析原页"
        code = "PAPER_PARSE_FAILED"
        try:
            with self.session_factory() as session:
                record = self._record(session, identity)
                if record.status != PaperImportStatus.UPLOADED:
                    return
                record.status = PaperImportStatus.PARSING
                actor_id = record.uploaded_by
                data = FileStorageService(
                    session, root=self.settings.storage_root
                ).read_document(record.document)
                session.commit()
            texts: list[TextPage] = []
            scans: list[tuple[UUID, int]] = []
            with open_paper(data) as source:
                with self.session_factory() as session:
                    self._record(session, identity).page_count = source.page_count
                    session.commit()
                for rendered in source.pages():
                    with self.session_factory() as session:
                        page = QuestionAssetService(
                            session, root=self.settings.storage_root
                        ).create_source_page(
                            identity,
                            page_number=rendered.page_number,
                            content=rendered.png,
                            actor_id=actor_id,
                        )
                        if rendered.needs_ocr:
                            scans.append((page.id, page.page_number))
                        else:
                            texts.append(
                                TextPage(
                                    page.id, page.page_number, rendered.text, "TEXT"
                                )
                            )
            with self.session_factory() as session:
                self._record(session, identity).status = PaperImportStatus.EXTRACTING
                session.commit()
            if scans:
                phase, code = "OCR", "PAPER_OCR_FAILED"
                ocr = self.ocr or create_ocr_provider(self.settings)
                for page_id, number in scans:
                    phase = f"第 {number} 页 OCR"
                    with self.session_factory() as session:
                        path, _ = FileStorageService(
                            session, root=self.settings.storage_root
                        ).download("p_" + page_id.hex, actor_id=actor_id)
                    result = await ocr.extract_text(path)
                    with self.session_factory() as session:
                        ocr_page = session.get(SourcePage, page_id)
                        assert ocr_page is not None
                        ocr_page.ocr_text, ocr_page.ocr_confidence = (
                            result.text,
                            result.confidence,
                        )
                        session.commit()
                    texts.append(TextPage(page_id, number, result.text, "OCR"))
            texts.sort(key=lambda page: page.page_number)
            phase, code = "分批拆题与结构化暂存", "PAPER_EXTRACTION_FAILED"
            extractor = self.extractor
            if extractor is None:
                owned_provider = create_llm_provider(self.settings)
                extractor = PaperExtractor(owned_provider)
            question_ids: list[UUID] = []
            page_ids = {page.page_number: str(page.id) for page in texts}
            async for batch in extractor.extract(texts):
                with self.session_factory() as session:
                    self._record(session, identity)
                    for item in batch.questions:
                        values = item.model_dump(mode="json")
                        question = ExtractedQuestion(
                            id=uuid4(),
                            paper_import_id=identity,
                            order_index=len(question_ids) + 1,
                            status=ExtractedQuestionStatus.PENDING_CORRECTION,
                            **values,
                        )
                        session.add(question)
                        question_ids.append(question.id)
                    for update in batch.updates:
                        existing_question = session.get(
                            ExtractedQuestion, question_ids[update.question_index - 1]
                        )
                        assert existing_question is not None
                        for name in ("reference_answer", "scoring_rubric", "analysis"):
                            value = getattr(update, name)
                            if value is not None:
                                if getattr(existing_question, name) not in (
                                    None,
                                    value,
                                ):
                                    raise ValueError(
                                        "answer update conflicts with previously extracted text"
                                    )
                                setattr(existing_question, name, value)
                        sources = set(existing_question.source_page_ids) | {
                            page_ids[e.page_number] for e in update.evidence.values()
                        }
                        existing_question.source_page_ids = [
                            page_ids[n]
                            for n in sorted(page_ids)
                            if page_ids[n] in sources
                        ]
                    session.commit()
            if not question_ids:
                raise PaperInputError(
                    "PAPER_EXTRACTION_FAILED", "没有识别到可校正的题目。"
                )
            with self.session_factory() as session:
                self._record(
                    session, identity
                ).status = PaperImportStatus.PENDING_REVIEW
                session.commit()
        except Exception as exc:  # noqa: BLE001 - persist the truthful terminal state of a background job.
            if isinstance(exc, PaperInputError):
                code, detail = exc.code, str(exc)
            elif isinstance(exc, PaperExtractionError):
                detail = f"原文来源校验失败：{exc}"
            elif isinstance(exc, ValidationError):
                locations = [
                    ".".join(str(p) for p in issue["loc"]) + ":" + issue["type"]
                    for issue in exc.errors(include_input=False, include_context=False)
                ]
                detail = "结构化输出校验失败：" + "；".join(locations)
            elif isinstance(exc, OCRProviderError):
                if exc.code == "OCR_PROVIDER_NOT_READY":
                    code = exc.code
                detail = str(exc)
            elif isinstance(exc, FileStorageError):
                code, detail = exc.code, str(exc)
            elif isinstance(exc, ProviderExecutionError):
                detail = f"{exc.code}：{exc.info.message}"
            elif isinstance(exc, ProviderCallError):
                detail = f"{exc.code}：{exc.safe_message}"
            else:
                detail = f"{type(exc).__name__}；处理或结构化校验失败，请核对原卷后重新导入。"
            logger.warning(
                "paper_import=%s phase=%s error=%s cause=%s",
                identity,
                phase,
                code,
                type(exc).__name__,
            )
            with self.session_factory() as session:
                record = self._record(session, identity)
                if record.status in ACTIVE:
                    record.status = PaperImportStatus.FAILED
                    record.error_code, record.error_message = (
                        code,
                        f"{phase}失败：{detail}",
                    )
                    session.commit()
        finally:
            if owned_provider is not None:
                await owned_provider.aclose()
