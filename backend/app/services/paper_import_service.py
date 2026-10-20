"""Paper source ingestion: durable stages, real progress, no knowledge indexing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, ClassVar
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

if TYPE_CHECKING:
    from backend.app.services.settings.runtime_snapshot import RunSnapshotContext

logger = logging.getLogger(__name__)
ACTIVE = {
    PaperImportStatus.UPLOADED,
    PaperImportStatus.PARSING,
    PaperImportStatus.EXTRACTING,
}


def _observed_provider_model(provider: object | None) -> str | None:
    """只读取 Provider 明确暴露的响应模型；未知保持未知。"""

    if provider is None:
        return None
    for name in ("last_response_model", "response_model", "returned_model"):
        value = getattr(provider, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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
        if record is None or record.subject_id is not None or record.course_id is None:
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
        runtime: RunSnapshotContext | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.extractor = extractor
        self.ocr = ocr
        self.runtime = runtime

    def _record(self, session: Session, identity: UUID) -> PaperImport:
        return session.scalars(
            select(PaperImport)
            .where(PaperImport.id == identity, PaperImport.subject_id.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()

    def recover_interrupted(self) -> int:
        with self.session_factory() as session:
            records = list(
                session.scalars(
                    select(PaperImport)
                    .where(PaperImport.status.in_(ACTIVE), PaperImport.subject_id.is_(None))
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
        provider_lease = None
        ocr_lease = None
        llm_attempted = False
        ocr_attempted = False
        phase = "解析原页"
        code = "PAPER_PARSE_FAILED"
        try:
            with self.session_factory() as session:
                record = self._record(session, identity)
                if record.status != PaperImportStatus.UPLOADED:
                    return
                record.status = PaperImportStatus.PARSING
                actor_id = record.uploaded_by
                if actor_id is None:
                    raise RuntimeError("PAPER_LEGACY_ACTOR_REQUIRED")
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
                if self.ocr is not None:
                    ocr = self.ocr
                elif self.runtime is not None:
                    ocr_lease = self.runtime.acquire("ocr")
                    ocr = ocr_lease.client
                else:
                    ocr = create_ocr_provider(self.settings)
                ocr_attempted = True
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
                if self.runtime is not None:
                    provider_lease = self.runtime.acquire("paper_extraction")
                    owned_provider = provider_lease.client
                else:
                    owned_provider = create_llm_provider(self.settings)
                extractor = PaperExtractor(owned_provider)
                llm_attempted = True
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
            if self.runtime is not None:
                if llm_attempted and provider_lease is not None:
                    self.runtime.record_invocation(
                        "paper_extraction",
                        returned_model=_observed_provider_model(owned_provider),
                    )
                if ocr_attempted and ocr_lease is not None:
                    self.runtime.record_invocation("ocr")
            if ocr_lease is not None:
                await ocr_lease.arelease()
            if provider_lease is not None:
                await provider_lease.arelease()
            elif owned_provider is not None:
                await owned_provider.aclose()


class PersonalPaperImportService:
    """Subject staging shares managed materials; it never creates a virtual teacher."""

    from threading import Lock

    _ocr_slot = Lock()
    _active_ocr: ClassVar[set[UUID]] = set()

    def __init__(self, space):
        from backend.app.services.learning_material_service import (
            LearningMaterialService,
        )

        self.space = space
        self.sessions = space.sessions
        self.materials = LearningMaterialService(space)

    def _record(self, session, identity, *, lock=False):
        from backend.app.services.learning_material_service import MaterialError

        self.space._check_space()
        query = select(PaperImport).where(
            PaperImport.id == identity, PaperImport.subject_id.is_not(None),
        )
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise MaterialError("PAPER_NOT_FOUND", 404)
        return row

    def create(self, subject_id, document_id):
        from backend.app.services.learning_material_service import MaterialError

        self.space._check_space()
        with self.sessions.begin() as session:
            document = session.scalar(select(Document).where(
                Document.id == document_id, Document.subject_id == subject_id,
            ).with_for_update())
            if document is None:
                raise MaterialError("MATERIAL_NOT_FOUND", 404)
            if document.purpose != DocumentPurpose.PAPER_SOURCE:
                raise MaterialError("PAPER_PURPOSE_REQUIRED", 409)
            if document.material_state != "parsed":
                raise MaterialError("PAPER_SOURCE_NOT_READY", 409)
            row = session.scalar(select(PaperImport).where(PaperImport.document_id == document_id))
            if row is None:
                row = PaperImport(
                    id=uuid4(), subject_id=subject_id, document_id=document_id,
                    original_filename=document.original_filename,
                    course_id=None, uploaded_by=None,
                    page_count=(document.parse_facts or {}).get("page_count"),
                    status=PaperImportStatus.UPLOADED,
                )
                session.add(row)
                session.flush()
            identity = row.id
        return self.get(identity)

    @staticmethod
    def _matches(source, block):
        return (
            (source.anchor is None or source.anchor == block.get("anchor"))
            and (source.page is None or source.page == block.get("page"))
        )

    def _catalog(self, document):
        from uuid import uuid5

        from backend.app.schemas.content_blocks import SourceLocation
        from backend.app.schemas.personal_learning import PersonalPaperAssetView

        facts = document.parse_facts or {}
        stored = {a.get("sha256") for a in facts.get("assets", []) if a.get("stored") is True}
        result = {}
        for block in facts.get("blocks", []):
            digest = block.get("sha256")
            if block.get("type") != "image" or digest not in stored:
                continue
            self.materials.asset_path(document.subject_id, document.id, digest)
            identity = uuid5(document.id, str(digest) + ":" + str(block["anchor"]))
            result[identity] = PersonalPaperAssetView(
                id=identity, sha256=digest, media_type=block["media_type"],
                source=SourceLocation(
                    document_id=document.id, page=block.get("page"), anchor=block["anchor"],
                ),
            )
        return result

    def get(self, identity):
        from backend.app.schemas.personal_learning import (
            PersonalPaperQuestionView,
            PersonalPaperView,
        )
        from backend.app.services.learning_material_service import MaterialError

        with self.sessions.begin() as session:
            paper = self._record(session, identity)
            if paper.status == PaperImportStatus.PARSING and identity not in self._active_ocr:
                paper.status = PaperImportStatus.FAILED
                paper.error_code = "PAPER_OCR_INTERRUPTED"
                paper.error_message = "上次本地 OCR 已中断；原文件仍保留，请明确重新处理。"

            document = session.get(Document, paper.document_id)
            if document is None or document.subject_id != paper.subject_id:
                raise MaterialError("MATERIAL_NOT_FOUND", 404)
            catalog = self._catalog(document)
            records = session.scalars(select(ExtractedQuestion).where(
                ExtractedQuestion.paper_import_id == identity,
            ).order_by(ExtractedQuestion.order_index, ExtractedQuestion.id)).all()
            return PersonalPaperView(
                id=paper.id, subject_id=paper.subject_id, document_id=paper.document_id,
                status=paper.status.value, error_code=paper.error_code,
                extraction_operation_id=paper.extraction_operation_id,
                assets=list(catalog.values()),
                questions=[PersonalPaperQuestionView(
                    **row.personal_content, id=row.id, revision=row.edit_revision,
                    status=row.status.value, question_id=row.question_id,
                ) for row in records],
            )

    def _validate_sources(self, value, document, catalog):
        from backend.app.schemas.content_blocks import OriginalBlock
        from backend.app.services.learning_material_service import MaterialError

        blocks = (document.parse_facts or {}).get("blocks", [])
        for source in value.source_locations:
            if source.document_id != document.id or not any(
                self._matches(source, block) for block in blocks
            ):
                raise MaterialError("PAPER_SOURCE_SCOPE_INVALID", 409)
        for evidence in value.evidence.values():
            if evidence.source not in value.source_locations:
                raise MaterialError("PAPER_SOURCE_SCOPE_INVALID", 409)
            text = "".join(
                str(block.get("text") or "")
                for block in blocks if self._matches(evidence.source, block)
            )
            if "".join(evidence.text.split()) not in "".join(text.split()):
                raise MaterialError("PAPER_SOURCE_EVIDENCE_INVALID", 409)
        for asset_id in value.asset_ids():
            asset = catalog.get(asset_id)
            if asset is None or not any(
                source.document_id == document.id
                and (source.page is None or source.page == asset.source.page)
                and (source.anchor is None or source.anchor == asset.source.anchor)
                for source in value.source_locations
            ):
                raise MaterialError("PAPER_ASSET_SCOPE_INVALID", 409)
        for block in value.blocks():
            if isinstance(block, OriginalBlock):
                asset = catalog.get(block.asset_id)
                if asset is None or block.source != asset.source:
                    raise MaterialError("PAPER_ASSET_SOURCE_INVALID", 409)
        if value.knowledge_point_ids:
            known = set(self._knowledge_ids(document.subject_id))
            if not set(value.knowledge_point_ids) <= known:
                raise MaterialError("KNOWLEDGE_POINT_NOT_FOUND", 404)

    def _knowledge_ids(self, subject_id):
        from backend.app.models.subject import KnowledgePoint

        with self.sessions() as session:
            return list(session.scalars(select(KnowledgePoint.id).where(
                KnowledgePoint.subject_id == subject_id,
                KnowledgePoint.confirmation_state == "confirmed",
            )))

    def store_candidates(self, identity, values, *, operation_id=None):
        from backend.app.ai.paper_extraction.schemas import PersonalPaperCandidate
        from backend.app.domain.enums import ExtractedBy
        from backend.app.services.learning_material_service import MaterialError

        candidates = [PersonalPaperCandidate.model_validate(v) for v in values]
        with self.sessions.begin() as session:
            paper = self._record(session, identity, lock=True)
            existing = session.scalars(select(ExtractedQuestion).where(
                ExtractedQuestion.paper_import_id == identity,
            )).all()
            if existing:
                if operation_id is not None and paper.extraction_operation_id == operation_id:
                    return self.get(identity)
                raise MaterialError("PAPER_ALREADY_EXTRACTED", 409)
            document = session.get(Document, paper.document_id)
            catalog = self._catalog(document)
            for value in candidates:
                self._validate_sources(value, document, catalog)
            blocks = (document.parse_facts or {}).get("blocks", [])
            def position(value):
                return min(
                    i for i, block in enumerate(blocks)
                    if any(self._matches(source, block) for source in value.source_locations)
                )
            candidates.sort(key=position)
            if (document.parse_facts or {}).get("ocr"):
                for candidate in candidates:
                    candidate.uncertainties = list(dict.fromkeys([*candidate.uncertainties, "OCR_REQUIRES_REVIEW"]))
            for index, value in enumerate(candidates, 1):
                session.add(ExtractedQuestion(
                    id=uuid4(), paper_import_id=identity, personal_content=value.model_dump(mode="json"),
                    order_index=index, edit_revision=1,
                    status=ExtractedQuestionStatus.PENDING_CORRECTION,
                    extracted_by=ExtractedBy.LLM if operation_id else ExtractedBy.TEXT,
                    source_page_ids=[],
                ))
            paper.status = PaperImportStatus.PENDING_REVIEW
            paper.error_code = None
            paper.error_message = None
        return self.get(identity)

    def prepare_extraction(self, identity, operations, idempotency_key, *, retry_of_id=None, acknowledge_unknown=False):
        from backend.app.services.learning_material_service import MaterialError
        from backend.app.services.learning_operations import StepPlan

        if retry_of_id is not None:
            with self.sessions() as session:
                paper = self._record(session, identity)
                if paper.extraction_operation_id != retry_of_id:
                    raise MaterialError("PAPER_OPERATION_CHANGED",409)
            operation = operations.retry(retry_of_id,idempotency_key=idempotency_key,
                                         acknowledge_unknown=acknowledge_unknown)
            with self.sessions.begin() as session:
                paper = self._record(session,identity,lock=True)
                if paper.extraction_operation_id not in (retry_of_id,operation.id):
                    raise MaterialError("PAPER_OPERATION_CHANGED",409)
                paper.extraction_operation_id=operation.id
            return operation
        with self.sessions() as session:
            paper = self._record(session, identity)
            if paper.extraction_operation_id is not None:
                return operations.view(paper.extraction_operation_id)
            document = session.get(Document, paper.document_id)
            facts = document.parse_facts or {}
            if facts.get("requires_ocr") or facts.get("unprocessed_pages"):
                raise MaterialError("PAPER_SOURCE_NEEDS_PROCESSING", 409)
            catalog = self._catalog(document)
            payload = {
                "document_id": str(document.id),
                "blocks": facts.get("blocks", []),
                "assets": [asset.model_dump(mode="json") for asset in catalog.values()],
                "allowed_knowledge_point_ids": [str(k) for k in self._knowledge_ids(document.subject_id)],
            }
        from backend.app.ai.paper_extraction.service import personal_extraction_messages

        messages = personal_extraction_messages(payload)
        operation = operations.prepare(
            purpose="paper_extraction:" + str(identity), idempotency_key=idempotency_key,
            steps=[StepPlan(step_id="paper", task_kind="paper_extraction",
                            messages=messages, request_budget=1)],
        )
        with self.sessions.begin() as session:
            paper = self._record(session, identity, lock=True)
            if paper.extraction_operation_id not in (None, operation.id):
                raise MaterialError("PAPER_OPERATION_CHANGED", 409)
            paper.extraction_operation_id = operation.id
        return operation

    async def execute_extraction(self, identity, operations, operation_id):
        from backend.app.ai.paper_extraction.schemas import PersonalPaperBatch
        from backend.app.services.learning_material_service import MaterialError

        with self.sessions() as session:
            paper = self._record(session, identity)
            if paper.extraction_operation_id != operation_id:
                raise MaterialError("PAPER_OPERATION_CHANGED", 409)
        from backend.app.ai.paper_extraction.service import extract_personal_step

        result = await operations.execute(operation_id, invoke=extract_personal_step)
        if result.state == "succeeded":
            batch = PersonalPaperBatch.model_validate(result.steps[0]["result"])
            return self.store_candidates(
                identity, [q.model_dump(mode="json") for q in batch.questions],
                operation_id=operation_id,
            )
        return result


    async def process_local_ocr(self, identity, snapshots, *, limits=None):
        """Explicit local-only OCR; the native worker owns its slot until it ends."""
        import asyncio
        from copy import deepcopy
        from threading import Event

        from backend.app.services.learning_material_service import MaterialError
        from backend.app.services.learning_operations import LearningLimits, digest

        limits = limits or LearningLimits()

        if not self._ocr_slot.acquire(blocking=False):
            raise MaterialError("PAPER_OCR_BUSY", 409)
        owns_slot = True
        try:
            with self.sessions() as session:
                paper = self._record(session, identity)
                if session.scalar(select(ExtractedQuestion.id).where(
                    ExtractedQuestion.paper_import_id == identity,
                ).limit(1)) is not None:
                    raise MaterialError("PAPER_ALREADY_EXTRACTED", 409)
                document = session.get(Document, paper.document_id)
                original = deepcopy(document.parse_facts or {})
                if not original.get("requires_ocr"):
                    self._ocr_slot.release()
                    owns_slot = False
                    return self.get(identity)
                document_id, subject_id = document.id, paper.subject_id
            snapshot = snapshots.create(
                workflow_id="personal-ocr:" + str(identity), request_id=uuid4().hex, task_kinds=("ocr",),
            )
            binding = snapshot.resolved_task_bindings["ocr"]
            if binding.adapter_id != "rapidocr":
                raise MaterialError("LOCAL_OCR_REQUIRED", 409)
            self._active_ocr.add(identity)
            with self.sessions.begin() as session:
                paper = self._record(session, identity, lock=True)
                paper.status = PaperImportStatus.PARSING
                paper.error_code = paper.error_message = None
        except BaseException:
            self._active_ocr.discard(identity)
            if owns_slot:
                self._ocr_slot.release()
            raise
        stopped = Event()
        expected = digest(original)

        async def work():
            runtime = None
            try:
                runtime = snapshots.context(snapshot)
                lease = runtime.acquire("ocr")
                facts = deepcopy(original)
                blocks = facts.get("blocks", [])
                known = {str(b.get("ocr_source_anchor")) for b in blocks if b.get("ocr_source_anchor")}
                text_pages = {b.get("page") for b in blocks if b.get("type") == "text"
                              and b.get("page") is not None and str(b.get("text") or "").strip()}
                result_blocks = []
                incomplete = False
                for block in blocks:
                    result_blocks.append(block)
                    if block.get("type") != "image" or block["anchor"] in known:
                        continue
                    if block.get("page") is not None and block.get("page") in text_pages:
                        continue
                    if stopped.is_set():
                        incomplete = True
                        continue
                    path, _ = self.materials.asset_path(subject_id, document_id, block["sha256"])
                    value = await lease.client.extract_text(path)
                    if not value.text.strip():
                        incomplete = True
                        continue
                    result_blocks.append({
                        "type":"text", "anchor":block["anchor"] + ":ocr",
                        "page":block.get("page"), "text":value.text,
                        "ocr_source_anchor":block["anchor"],
                    })
                    facts.setdefault("ocr", {})[block["anchor"]] = {
                        "snapshot_id":str(snapshot.snapshot_id), "adapter":"rapidocr",
                        "model":binding.model_id, "regions":[r.model_dump(mode="json") for r in value.regions],
                        "confidence":str(value.confidence) if value.confidence is not None else None,
                    }
                facts["blocks"] = result_blocks
                facts["requires_ocr"] = incomplete
                facts["issues"] = list(dict.fromkeys([
                    *[issue for issue in facts.get("issues", [])
                      if issue not in {"PDF_REQUIRES_OCR","DOCX_IMAGE_ONLY_REQUIRES_OCR","OCR_SOFT_TIMEOUT"}],
                    "OCR_REQUIRES_REVIEW",
                    *(["OCR_SOFT_TIMEOUT"] if stopped.is_set() else []),
                ]))
                with self.sessions.begin() as session:
                    paper = self._record(session, identity, lock=True)
                    document = session.get(Document, document_id, with_for_update=True)
                    if digest(document.parse_facts or {}) != expected:
                        raise MaterialError("PAPER_SOURCE_CHANGED", 409)
                    document.parse_facts = facts
                    paper.status = PaperImportStatus.UPLOADED
                return self.get(identity)
            except Exception as error:
                with self.sessions.begin() as session:
                    paper = self._record(session, identity, lock=True)
                    paper.status = PaperImportStatus.FAILED
                    paper.error_code = getattr(error, "code", "PAPER_OCR_FAILED")
                    paper.error_message = "本地 OCR 未完成；原文件和已有解析事实仍保留，请检查本地依赖。"
                raise
            finally:
                if runtime is not None:
                    await runtime.aclose()

        def worker():
            try:
                # A dedicated worker loop retains the native OCR await even if
                # the HTTP request's loop is cancelled or its soft wait expires.
                return asyncio.run(work())
            finally:
                self._active_ocr.discard(identity)
                self._ocr_slot.release()

        task = asyncio.create_task(asyncio.to_thread(worker))
        self.space.local_ocr_tasks.add(task)
        def finished(done):
            self.space.local_ocr_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # retrieve safely; the domain failure was persisted
        task.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=limits.local_wait_seconds)
        except TimeoutError:
            stopped.set()
            return self.get(identity)
        except asyncio.CancelledError:
            stopped.set()
            raise
