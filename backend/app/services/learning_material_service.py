"""Personal learning materials: upload purpose, managed bytes, parse and index state.

Only user uploads become material (FR-003). This module never fetches a URL,
crawls a textbook or downloads anything; it has no HTTP client at all.

Two upload purposes stay separated (FR-005):

- ``study_material``: parsed, chunked into the subject's personal knowledge base
  and only retrievable after an explicit, identity-checked index step.
- ``exam_paper``: the original file and its parse facts are retained, but no
  teaching chunk is created, so paper answers cannot silently become teaching
  RAG content.

Paid steps never run implicitly: parsing and chunking are local, and
``index_material`` is the explicit entry point a later, user-confirmed
operation would call. A material that is parsed but not indexed is reported as
``parsed``; it is never presented as retired/ready content.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.ai.ingestion.chunking import chunk_document
from backend.app.ai.ingestion.cleaning import clean_document
from backend.app.ai.ingestion.parsers import (
    DEFAULT_REGISTRY,
    DOCUMENT_CORRUPTED,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_SIZE_LIMIT,
    DOCUMENT_UNSUPPORTED_FORMAT,
    DocumentParseError,
    ParsedBlock,
    ParsedDocument,
    ParsedSection,
    detect_file_extension,
)
from backend.app.domain.enums import DocumentPurpose, DocumentStatus
from backend.app.models.document import Document
from backend.app.models.document_chunk import (
    EMBEDDING_VECTOR_DIMENSION,
    DocumentChunk,
)
from backend.app.models.knowledge_base import KnowledgeBase
from backend.app.models.subject import MaterialSeries, MaterialVersion, Subject
from backend.app.schemas.personal_learning import (
    MaterialAssetView,
    MaterialBlockView,
    MaterialList,
    MaterialPurpose,
    MaterialSeriesList,
    MaterialSeriesView,
    MaterialUpload,
    MaterialVersionInput,
    MaterialVersionView,
    MaterialView,
)
from backend.app.services.file_storage_service import (
    FileStorageService,
    personal_material_asset_locator,
)
from backend.app.services.learning_operations import LearningLimits
from backend.app.services.settings.index_identity import (
    IndexIdentityError,
    IndexIdentityService,
    SqlAlchemyIndexRepository,
    SqlAlchemyIndexUnitOfWork,
)

#: 学习资料的知识库名称；一个科目只有一个个人教学知识库。
SUBJECT_KNOWLEDGE_BASE_NAME = "资料知识库"
#: 试卷用途不得建立教学片段。
PAPER_REJECTED_CODES = frozenset(
    {
        "MATERIAL_PURPOSE_MISMATCH",
        "MATERIAL_NOT_STUDY_MATERIAL",
    }
)


class MaterialError(RuntimeError):
    """资料操作失败；携带机器可读码与真实 HTTP 语义。"""

    def __init__(self, code: str, status: int = 422) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _parse_status(error_code: str | None) -> int:
    if error_code in {DOCUMENT_SIZE_LIMIT}:
        return 413
    if error_code in {DOCUMENT_UNSUPPORTED_FORMAT}:
        return 415
    return 422


class LearningMaterialService:
    """Subject-scoped material lifecycle on the verified personal space."""

    def __init__(
        self,
        space: Any,
        *,
        limits: LearningLimits | None = None,
        owned_engine: Engine | None = None,
    ) -> None:
        self.space = space
        self.sessions: sessionmaker[Session] = space.sessions
        self.storage_root: Path = space.storage_root
        self.profile = space.profile
        self.limits = limits or LearningLimits()
        self.owned_engine = owned_engine

    # ------------------------------------------------------------------ read
    def list(self, subject_id: UUID) -> MaterialList:
        self.space._check_space()
        with self.sessions() as session:
            self._subject(session, subject_id)
            rows = session.scalars(
                select(Document)
                .where(Document.subject_id == subject_id)
                .order_by(Document.created_at, Document.id)
            ).all()
            return MaterialList(
                materials=[self._view(session, row) for row in rows]
            )

    def get(self, material_id: UUID, *, subject_id: UUID | None = None) -> MaterialView:
        self.space._check_space()
        with self.sessions() as session:
            row = session.get(Document, material_id)
            if row is None or row.subject_id is None:
                raise MaterialError("MATERIAL_NOT_FOUND", 404)
            if subject_id is not None and row.subject_id != subject_id:
                # Cross-subject reads are indistinguishable from missing rows.
                raise MaterialError("MATERIAL_NOT_FOUND", 404)
            return self._view(session, row)

    def asset_path(
        self, subject_id: UUID, material_id: UUID, digest: str
    ) -> tuple[Path, str]:
        """Resolve one stored parse asset inside the verified space.

        Only a digest already recorded as a stored asset of this material in
        this subject is readable; every other case is the same 404, so the
        route cannot be used to probe other spaces, materials or files.
        """

        self.space._check_space()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise MaterialError("MATERIAL_ASSET_NOT_FOUND", 404)
        with self.sessions() as session:
            row = session.get(Document, material_id)
            if row is None or row.subject_id != subject_id:
                raise MaterialError("MATERIAL_NOT_FOUND", 404)
            facts = row.parse_facts or {}
            asset = next(
                (
                    item
                    for item in facts.get("assets") or []
                    if item.get("sha256") == digest and item.get("stored") is True
                ),
                None,
            )
            if asset is None:
                raise MaterialError("MATERIAL_ASSET_NOT_FOUND", 404)
            media_type = str(asset.get("media_type") or "application/octet-stream")
            storage = FileStorageService(session, profile=self.profile)
            path = storage.resolve_path(
                personal_material_asset_locator(material_id, digest, media_type)
            )
        if not path.is_file():
            raise MaterialError("MATERIAL_ASSET_NOT_FOUND", 404)
        try:
            with path.open("rb") as source:
                actual_digest = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError:
            raise MaterialError("MATERIAL_ASSET_NOT_FOUND", 404) from None
        if actual_digest != digest:
            raise MaterialError("MATERIAL_ASSET_CORRUPTED", 409)
        return path, media_type

    # ---------------------------------------------------------------- upload
    def upload(
        self,
        subject_id: UUID,
        *,
        filename: str,
        path: Path,
        size: int,
        sha256: str,
        body: MaterialUpload,
    ) -> MaterialView:
        """Register a streamed upload, parse locally and expose real state.

        The transport already enforced the byte cap while streaming; this method
        re-checks the declared size, stores the managed original from the temp
        path, then parses at most one approved page batch.
        """

        self.space._check_space()
        if not isinstance(size, int) or size <= 0:
            raise MaterialError("DOCUMENT_EMPTY", 422)
        if size > self.limits.upload_bytes:
            raise MaterialError(DOCUMENT_SIZE_LIMIT, 413)
        name = (filename or "").strip()
        if not name or len(name) > 255:
            raise MaterialError("REQUEST_INVALID", 422)
        file_format = detect_file_extension(name)
        if DEFAULT_REGISTRY.format_for(name) is None:
            raise MaterialError(DOCUMENT_UNSUPPORTED_FORMAT, 415)

        purpose = (
            DocumentPurpose.KNOWLEDGE_BASE
            if body.builds_teaching_index
            else DocumentPurpose.PAPER_SOURCE
        )
        material_id = uuid4()
        with self.sessions.begin() as session:
            self._subject(session, subject_id)
            knowledge_base_id = None
            if purpose is DocumentPurpose.KNOWLEDGE_BASE:
                knowledge_base_id = self._personal_knowledge_base(
                    session, subject_id
                ).id
            row = Document(
                id=material_id,
                subject_id=subject_id,
                course_id=None,
                knowledge_base_id=knowledge_base_id,
                uploaded_by=None,
                purpose=purpose,
                original_filename=name,
                file_format=file_format,
                status=DocumentStatus.UPLOADED,
                material_state="uploaded",
                optional_metadata=body.optional_metadata() or None,
                retryable=False,
            )
            session.add(row)
            session.flush()
            stored = FileStorageService(
                session, profile=self.profile
            ).store_personal_document_from_path(
                row, path, size=size, sha256=sha256
            )
            row.storage_path = stored.storage_path
            row.file_metadata = stored.metadata.model_dump(mode="json")

        data = path.read_bytes()
        try:
            parsed = (
                self._page_batch(data, start_page=body.start_page or 1)
                if file_format == "pdf"
                else DEFAULT_REGISTRY.parse(name, data)
            )
        except DocumentParseError as error:
            self._mark_failed(material_id, error.error_code, error.user_message, error.retryable)
            raise MaterialError(error.error_code, _parse_status(error.error_code)) from None
        except MaterialError as error:
            self._mark_failed(
                material_id, error.code, "PDF page processing failed.",
                error.code in {DOCUMENT_PARSE_FAILED, "PAPER_PARSE_FAILED"},
            )
            raise

        self._persist_parse(
            material_id,
            parsed,
            purpose=purpose,
            unprocessed_pages=parsed.unprocessed_pages,
            page_count=parsed.page_count,
        )
        return self.get(material_id, subject_id=subject_id)

    def _page_batch(self, data: bytes, *, start_page: int) -> ParsedDocument:
        """Persist only pages actually rendered within the approved batch/budget."""

        from backend.app.ai.ingestion.paper_pipeline import (
            PaperInputError,
            open_paper,
        )

        if not data.lstrip().startswith(b"%PDF-"):
            raise DocumentParseError(DOCUMENT_CORRUPTED)

        blocks: list[ParsedBlock] = []
        sections: list[ParsedSection] = []
        requires_ocr = False
        try:
            with open_paper(
                data,
                batch_pages=self.limits.pdf_batch_pages,
                decoded_rgba_bytes=self.limits.decoded_rgba_bytes,
                start_page=start_page,
            ) as paper:
                total = paper.page_count
                unprocessed = (
                    *range(1, start_page),
                    *paper.unprocessed_pages,
                )
                for page in paper.pages():
                    anchor = f"第 {page.page_number} 页"
                    requires_ocr = requires_ocr or page.needs_ocr
                    if page.text.strip():
                        sections.append(ParsedSection(
                            index=page.page_number, location=anchor, text=page.text,
                        ))
                        blocks.append(ParsedBlock(
                            kind="text", anchor=anchor, page=page.page_number,
                            text=page.text,
                        ))
                    blocks.append(ParsedBlock(
                        kind="image", anchor=anchor, page=page.page_number,
                        image=page.png, media_type="image/png",
                        sha256=hashlib.sha256(page.png).hexdigest(),
                        width=page.width, height=page.height,
                    ))
        except PaperInputError as error:
            code = DOCUMENT_PARSE_FAILED if error.code == "PAPER_PARSE_FAILED" else error.code
            raise MaterialError(code, 422) from error
        except Exception as error:
            raise MaterialError(DOCUMENT_PARSE_FAILED, 422) from error
        issues = []
        if requires_ocr:
            issues.append("PDF_REQUIRES_OCR")
        if unprocessed:
            issues.append("PDF_BATCH_INCOMPLETE")
        return ParsedDocument(
            file_format="pdf",
            text='\n\n'.join(section.text for section in sections),
            sections=tuple(sections),
            page_count=total,
            blocks=tuple(blocks),
            requires_ocr=requires_ocr,
            issues=tuple(issues),
            unprocessed_pages=unprocessed,
        )

    def _persist_parse(
        self,
        material_id: UUID,
        parsed: ParsedDocument,
        *,
        purpose: DocumentPurpose,
        unprocessed_pages: tuple[int, ...] = (),
        page_count: int | None = None,
    ) -> None:
        """Store assets, chunks and parse facts; nothing is silently dropped."""

        with self.sessions.begin() as session:
            row = session.get(Document, material_id)
            if row is None:
                raise MaterialError("MATERIAL_NOT_FOUND", 404)
            storage = FileStorageService(session, profile=self.profile)
            assets: list[dict[str, Any]] = []
            seen: set[str] = set()
            for block in parsed.blocks:
                if block.kind != "image" or block.image is None or not block.sha256:
                    continue
                if block.sha256 in seen:
                    assets.append(
                        _asset_fact(block, stored=block.sha256 in seen)
                    )
                    continue
                if block.media_type not in {"image/png", "image/jpeg"}:
                    # 无法安全解码/渲染的原始图保留在原件里并记疑点，不复制成媒体。
                    assets.append(_asset_fact(block, stored=False))
                    continue
                stored = storage.store_personal_material_asset(
                    row, block.image, media_type=str(block.media_type)
                )
                seen.add(str(block.sha256))
                assets.append(
                    _asset_fact(block, stored=True, locator=stored.storage_path)
                )

            facts: dict[str, Any] = {
                "file_format": parsed.file_format,
                "page_count": page_count if page_count is not None else parsed.page_count,
                "requires_ocr": parsed.requires_ocr,
                "issues": list(parsed.issues),
                "unprocessed_pages": list(unprocessed_pages),
                "blocks": [_block_fact(block) for block in parsed.blocks],
                "assets": assets,
                "chunk_count": 0,
            }
            chunk_count = 0
            if purpose is DocumentPurpose.KNOWLEDGE_BASE and row.knowledge_base_id:
                chunk_count = self._write_chunks(
                    session, row, parsed, knowledge_base_id=row.knowledge_base_id
                )
            facts["chunk_count"] = chunk_count
            row.parse_facts = facts
            row.error_code = None
            row.error_message = None
            row.retryable = False
            row.material_state = "parsed"
            # 解析完成不等于索引完成：Ready 只由显式索引步骤设置。
            row.status = DocumentStatus.CHUNKING

    def _write_chunks(
        self,
        session: Session,
        row: Document,
        parsed: ParsedDocument,
        *,
        knowledge_base_id: UUID,
    ) -> int:
        cleaned = clean_document(parsed)
        if not cleaned.sections:
            return 0
        chunks = chunk_document(
            cleaned,
            document_id=row.id,
            knowledge_base_id=knowledge_base_id,
            subject_id=row.subject_id,
            original_filename=row.original_filename,
            max_chars=500,
            overlap_chars=80,
        )
        postgres = session.get_bind().dialect.name == "postgresql"
        for chunk in chunks:
            metadata = chunk.as_metadata()
            metadata["purpose"] = row.purpose.value
            session.add(
                DocumentChunk(
                    document_id=row.id,
                    course_id=None,
                    subject_id=row.subject_id,
                    knowledge_base_id=knowledge_base_id,
                    chapter_id=None,
                    section_order=None,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    embedding=None,
                    search_vector=(
                        func.to_tsvector("simple", chunk.content)
                        if postgres
                        else None
                    ),
                    chunk_metadata=metadata,
                )
            )
        session.flush()
        return len(chunks)

    def _mark_failed(
        self, material_id: UUID, code: str, message: str, retryable: bool
    ) -> None:
        with self.sessions.begin() as session:
            row = session.get(Document, material_id)
            if row is None:
                return
            row.status = DocumentStatus.FAILED
            row.material_state = "failed"
            row.error_code = code
            row.error_message = message
            row.retryable = retryable

    # ----------------------------------------------------------------- index
    def index_material(
        self,
        material_id: UUID,
        *,
        identity: Mapping[str, Any],
        vectors: Sequence[Sequence[float]],
    ) -> MaterialView:
        """Explicit, identity-checked index publication.

        Real paid Embedding is a separate user-confirmed operation; this method
        only accepts already-produced vectors, rejects half products and never
        rewrites an existing incompatible vector space.
        """

        self.space._check_space()
        with self.sessions.begin() as session:
            row = session.get(Document, material_id)
            if row is None or row.subject_id is None:
                raise MaterialError("MATERIAL_NOT_FOUND", 404)
            if row.purpose is not DocumentPurpose.KNOWLEDGE_BASE:
                raise MaterialError("MATERIAL_NOT_STUDY_MATERIAL", 409)
            if row.knowledge_base_id is None:
                raise MaterialError("MATERIAL_KNOWLEDGE_BASE_MISSING", 409)
            if row.material_state == "indexed":
                raise MaterialError("MATERIAL_ALREADY_INDEXED", 409)
            chunks = session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == row.id)
                .order_by(DocumentChunk.chunk_index)
            ).all()
            if not chunks:
                raise MaterialError("MATERIAL_HAS_NO_CHUNKS", 409)
            if len(vectors) != len(chunks):
                raise MaterialError("EMBEDDING_COUNT_MISMATCH", 409)
            dimension = EMBEDDING_VECTOR_DIMENSION
            for vector in vectors:
                if len(vector) != dimension:
                    raise MaterialError("EMBEDDING_DIMENSION_MISMATCH", 409)
            if int(identity.get("dimension") or 0) != dimension:
                raise MaterialError("EMBEDDING_DIMENSION_MISMATCH", 409)

            kb = session.get(KnowledgeBase, row.knowledge_base_id)
            if kb is None:
                raise MaterialError("MATERIAL_KNOWLEDGE_BASE_MISSING", 409)
            identity_service = IndexIdentityService(
                repository=SqlAlchemyIndexRepository(session)
            )
            try:
                if kb.embedding_identity is None:
                    # 只有“尚无任何向量”时才能登记身份；已有向量而身份未知必须先走
                    # 受控接纳，不能借新一次索引绕过身份检查。
                    existing = session.scalar(
                        select(func.count(DocumentChunk.id)).where(
                            DocumentChunk.knowledge_base_id == kb.id,
                            DocumentChunk.embedding.is_not(None),
                        )
                    )
                    if existing:
                        raise MaterialError("EMBEDDING_INCOMPATIBLE", 409)
                    # 复用现有身份端口写入本次登记事实；不另开事务，避免提交外层索引事务。
                    SqlAlchemyIndexUnitOfWork(
                        SqlAlchemyIndexRepository(session), kb
                    ).write_identity(
                        {
                            **dict(identity),
                            "dimension": dimension,
                            "status": "native",
                            "identity_revision": 1,
                        }
                    )
                else:
                    identity_service.require_compatible(
                        kb_id=kb.id, identity=identity
                    )
            except MaterialError:
                raise
            except IndexIdentityError as error:
                raise MaterialError(
                    "EMBEDDING_INCOMPATIBLE", 409
                ) from error

            postgres = session.get_bind().dialect.name == "postgresql"
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk.embedding = [float(value) for value in vector]
                if postgres:
                    chunk.search_vector = func.to_tsvector(
                        "simple", chunk.content
                    )
            row.status = DocumentStatus.READY
            row.material_state = "indexed"
            row.error_code = None
            row.error_message = None
            row.retryable = False
        return self.get(material_id)

    # -------------------------------------------------------------- versions
    def create_version(
        self, subject_id: UUID, body: MaterialVersionInput
    ) -> MaterialSeriesView:
        self.space._check_space()
        with self.sessions.begin() as session:
            self._subject(session, subject_id)
            document = session.get(Document, body.document_id)
            if document is None or document.subject_id != subject_id:
                raise MaterialError("MATERIAL_NOT_FOUND", 404)
            if document.purpose is not DocumentPurpose.KNOWLEDGE_BASE:
                raise MaterialError("MATERIAL_NOT_STUDY_MATERIAL", 409)
            if document.material_state == "failed":
                raise MaterialError("MATERIAL_NOT_READY", 409)
            if body.series_id is not None:
                series = session.get(MaterialSeries, body.series_id)
                if series is None or series.subject_id != subject_id:
                    # 不同科目/不存在一律视为不可用，不暴露另一个科目。
                    raise MaterialError("MATERIAL_SERIES_NOT_FOUND", 404)
            else:
                if not body.series_name:
                    raise MaterialError("MATERIAL_SERIES_NAME_REQUIRED", 422)
                series = MaterialSeries(
                    id=uuid4(), subject_id=subject_id, name=body.series_name
                )
                session.add(series)
                session.flush()
            duplicate = session.scalar(
                select(MaterialVersion.id).where(
                    MaterialVersion.series_id == series.id,
                    MaterialVersion.label == body.label,
                )
            )
            if duplicate is not None:
                raise MaterialError("MATERIAL_VERSION_DUPLICATE", 409)
            version = MaterialVersion(
                id=uuid4(),
                series_id=series.id,
                subject_id=subject_id,
                document_id=document.id,
                label=body.label,
            )
            session.add(version)
            session.flush()
            if series.default_version_id is None:
                # 用户确认的第一个版本成为默认；后续版本必须显式切换。
                series.default_version_id = version.id
            session.flush()
            return self._series_view(session, series)

    def list_series(self, subject_id: UUID) -> MaterialSeriesList:
        self.space._check_space()
        with self.sessions() as session:
            self._subject(session, subject_id)
            rows = session.scalars(
                select(MaterialSeries)
                .where(MaterialSeries.subject_id == subject_id)
                .order_by(MaterialSeries.created_at, MaterialSeries.id)
            ).all()
            return MaterialSeriesList(
                series=[self._series_view(session, row) for row in rows]
            )

    def set_default_version(
        self, series_id: UUID, version_id: UUID
    ) -> MaterialSeriesView:
        self.space._check_space()
        with self.sessions.begin() as session:
            series = session.get(MaterialSeries, series_id, with_for_update=True)
            if series is None:
                raise MaterialError("MATERIAL_SERIES_NOT_FOUND", 404)
            version = session.get(MaterialVersion, version_id)
            if (
                version is None
                or version.series_id != series.id
                or version.subject_id != series.subject_id
            ):
                raise MaterialError("MATERIAL_VERSION_NOT_IN_SERIES", 409)
            # 只改系列默认指针；版本行与历史来源保持不可变。
            series.default_version_id = version.id
            session.flush()
            return self._series_view(session, series)

    # ---------------------------------------------------------------- helpers
    def _subject(self, session: Session, subject_id: UUID) -> Subject:
        row = session.get(Subject, subject_id)
        if row is None:
            raise MaterialError("SUBJECT_NOT_FOUND", 404)
        return row

    def _personal_knowledge_base(
        self, session: Session, subject_id: UUID
    ) -> KnowledgeBase:
        row = session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.subject_id == subject_id)
        )
        if row is not None:
            return row
        row = KnowledgeBase(
            id=uuid4(),
            subject_id=subject_id,
            course_id=None,
            name=SUBJECT_KNOWLEDGE_BASE_NAME,
        )
        session.add(row)
        try:
            session.flush()
        except Exception:  # 并发创建时回落到已有行。
            session.rollback()
            existing = session.scalar(
                select(KnowledgeBase).where(
                    KnowledgeBase.subject_id == subject_id
                )
            )
            if existing is None:
                raise
            return existing
        return row

    def _series_view(
        self, session: Session, series: MaterialSeries
    ) -> MaterialSeriesView:
        versions = session.scalars(
            select(MaterialVersion)
            .where(MaterialVersion.series_id == series.id)
            .order_by(MaterialVersion.created_at, MaterialVersion.id)
        ).all()
        return MaterialSeriesView(
            id=series.id,
            subject_id=series.subject_id,
            name=series.name,
            default_version_id=series.default_version_id,
            versions=[
                MaterialVersionView(
                    id=row.id,
                    document_id=row.document_id,
                    label=row.label,
                    is_default=row.id == series.default_version_id,
                )
                for row in versions
            ],
        )

    def _view(self, session: Session, row: Document) -> MaterialView:
        facts = row.parse_facts or {}
        chunk_count = session.scalar(
            select(func.count(DocumentChunk.id)).where(
                DocumentChunk.document_id == row.id
            )
        ) or 0
        version = session.scalar(
            select(MaterialVersion).where(MaterialVersion.document_id == row.id)
        )
        series = session.get(MaterialSeries, version.series_id) if version else None
        issues = list(facts.get("issues") or [])
        failed = (row.material_state or "") == "failed"
        if failed:
            parse_state = "failed"
        elif issues or facts.get("requires_ocr") or facts.get("unprocessed_pages"):
            parse_state = "partial"
        else:
            parse_state = "parsed"
        state = row.material_state or ("failed" if failed else "uploaded")
        if state not in {"uploaded", "parsed", "indexed", "failed"}:
            state = "uploaded"
        # 个人资料始终属于一个科目；旧课程资料没有科目，不会走本视图。
        subject_id = row.subject_id
        if subject_id is None:
            raise MaterialError("MATERIAL_SCOPE_INVALID", 409)
        return MaterialView(
            id=row.id,
            subject_id=subject_id,
            purpose=(
                MaterialPurpose.STUDY_MATERIAL
                if row.purpose is DocumentPurpose.KNOWLEDGE_BASE
                else MaterialPurpose.EXAM_PAPER
            ),
            original_filename=row.original_filename,
            file_format=row.file_format,
            state=state,  # type: ignore[arg-type]
            parse_state=parse_state,  # type: ignore[arg-type]
            error_code=row.error_code,
            error_message=row.error_message,
            retryable=row.retryable,
            optional_metadata=dict(row.optional_metadata or {}),
            chunk_count=int(chunk_count),
            issues=issues,
            requires_ocr=bool(facts.get("requires_ocr")),
            blocks=[
                MaterialBlockView(**block) for block in facts.get("blocks") or []
            ],
            assets=[
                MaterialAssetView(**asset) for asset in facts.get("assets") or []
            ],
            series_id=series.id if series else None,
            version_id=version.id if version else None,
            version_label=version.label if version else None,
            default_version_id=series.default_version_id if series else None,
        )

    def close(self) -> None:
        if self.owned_engine is not None:
            self.owned_engine.dispose()


def _block_fact(block: ParsedBlock) -> dict[str, Any]:
    """Project a parsed block into a JSON fact without embedding raw bytes."""

    return {
        "type": block.kind,
        "anchor": block.anchor,
        "page": block.page,
        "text": block.text,
        "rows": [list(row) for row in block.rows],
        "mathml": block.mathml,
        "expression": block.expression,
        "sha256": block.sha256,
        "media_type": block.media_type,
        "placement": block.placement,
        "reason": block.reason,
    }


def _asset_fact(
    block: ParsedBlock, *, stored: bool, locator: str | None = None
) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "sha256": block.sha256,
        "media_type": block.media_type or "application/octet-stream",
        "anchor": block.anchor,
        "page": block.page,
        "stored": stored,
    }
    if locator is not None:
        fact["locator"] = locator
    return fact


def dumps(value: Any) -> str:
    """Deterministic JSON for tests and evidence without leaking file bytes."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


__all__ = [
    "PAPER_REJECTED_CODES",
    "SUBJECT_KNOWLEDGE_BASE_NAME",
    "LearningMaterialService",
    "MaterialError",
    "dumps",
]
