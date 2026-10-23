"""Local material OCR: frozen preview, native worker ownership, explicit review.

The existing RapidOCR snapshot/lease and paper OCR concurrency slot are reused.
There is no remote provider or automatic retry path in this module.
"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from threading import Event
from uuid import UUID, uuid4

from sqlalchemy import func, select

from backend.app.ai.ingestion.parsers import ParsedDocument, ParsedSection
from backend.app.core.maintenance import ensure_storage_writable
from backend.app.domain.enums import DocumentPurpose
from backend.app.models.document import Document
from backend.app.models.document_chunk import DocumentChunk
from backend.app.schemas.material_ocr import MaterialOCRView
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import digest
from backend.app.services.operation_executor import ExecutorError, ExecutorGuard

logger = logging.getLogger(__name__)


def source_digest(facts):
    return digest({key: value for key, value in facts.items() if key != "local_ocr"})


def ocr_candidates(facts):
    blocks = facts.get("blocks") or []
    known = {block["ocr_source_anchor"] for block in blocks if block.get("ocr_source_anchor")}
    text_pages = {block.get("page") for block in blocks if block.get("type") == "text"
                  and not block.get("ocr_source_anchor") and block.get("page") is not None
                  and str(block.get("text") or "").strip()}
    return [block for block in blocks if block.get("type") == "image"
            and block.get("anchor") not in known and block.get("page") not in text_pages]


class MaterialOCR:
    def __init__(self, materials, operations):
        self.materials = materials
        self.operations = operations
        self.sessions = materials.sessions
        # Independent local OCR has no paid LearningOperations Invocation. Its
        # space-owned snapshot service constructs local providers directly and
        # is closed with the SubjectService after all native work has ended.
        self.snapshots = materials.space.runtime_snapshots
        if self.snapshots is None:
            raise MaterialError("MATERIAL_OCR_UNAVAILABLE", 503)

    def _row(self, session, identity, *, lock=False):
        self.materials.space._check_space()
        row = session.get(Document, identity, with_for_update=lock)
        if row is None or row.subject_id is None:
            raise MaterialError("MATERIAL_NOT_FOUND", 404)
        self.materials._subject(session, row.subject_id)
        if row.purpose != DocumentPurpose.KNOWLEDGE_BASE:
            raise MaterialError("MATERIAL_NOT_STUDY_MATERIAL", 409)
        if row.material_state == "indexed":
            raise MaterialError("MATERIAL_ALREADY_INDEXED", 409)
        return row

    def _preview(self, row, preview_id):
        value = (row.parse_facts or {}).get("local_ocr")
        if value is None or value.get("id") != str(preview_id):
            raise MaterialError("MATERIAL_OCR_PREVIEW_NOT_FOUND", 404)
        return deepcopy(value)

    def _validate(self, row, value):
        if source_digest(row.parse_facts or {}) != value["source_digest"]:
            raise MaterialError("PREVIEW_CHANGED", 409)
        snapshot = self.snapshots.load(UUID(value["runtime_snapshot_id"]))
        active = self.operations.config.active_revision()
        if active is None or active.revision_id != snapshot.settings_revision:
            raise MaterialError("PREVIEW_CHANGED", 409)
        if snapshot.resolved_task_bindings["ocr"].adapter_id != "rapidocr":
            raise MaterialError("LOCAL_OCR_REQUIRED", 409)
        return snapshot

    def prepare(self, identity, idempotency_key):
        ensure_storage_writable(self.materials.storage_root)
        with self.sessions() as session:
            row = self._row(session, identity)
            facts = row.parse_facts or {}
            previous = facts.get("local_ocr")
            if previous and (previous["state"] in {"running", "review_required"}
                             or previous.get("idempotency_key") == idempotency_key):
                return MaterialOCRView.model_validate(previous)
            if not facts.get("requires_ocr") or not ocr_candidates(facts):
                raise MaterialError("MATERIAL_OCR_NOT_REQUIRED", 409)
            frozen = source_digest(facts)
            count = len(ocr_candidates(facts))
        snapshot = self.snapshots.create(workflow_id="material-ocr:" + str(identity),
            request_id=uuid4().hex, task_kinds=("ocr",), storage_root=self.materials.storage_root)
        binding = snapshot.resolved_task_bindings["ocr"]
        if binding.adapter_id != "rapidocr":
            raise MaterialError("LOCAL_OCR_REQUIRED", 409)
        value = {"id": str(uuid4()), "material_id": str(identity),
                 "runtime_snapshot_id": str(snapshot.snapshot_id), "state": "prepared",
                 "page_count": count, "completed_pages": 0, "adapter": "rapidocr",
                 "model": binding.model_id, "request_budget": 0, "error_code": None,
                 "source_digest": frozen, "idempotency_key": idempotency_key}
        value["preview_digest"] = digest(value)
        with self.sessions.begin() as session:
            row = self._row(session, identity, lock=True)
            current = (row.parse_facts or {}).get("local_ocr")
            if source_digest(row.parse_facts or {}) != frozen or current != previous:
                raise MaterialError("PREVIEW_CHANGED", 409)
            row.parse_facts = {**row.parse_facts, "local_ocr": value}
        return MaterialOCRView.model_validate(value)

    def confirm(self, identity, preview_id, preview_digest):
        ensure_storage_writable(self.materials.storage_root)
        with self.sessions.begin() as session:
            row = self._row(session, identity, lock=True)
            value = self._preview(row, preview_id)
            if value["preview_digest"] != preview_digest:
                raise MaterialError("PREVIEW_CHANGED", 409)
            if value["state"] in {"prepared", "confirmed"}:
                self._validate(row, value)
                value["state"] = "confirmed"
                row.parse_facts = {**row.parse_facts, "local_ocr": value}
        return MaterialOCRView.model_validate(value)

    def review(self, identity, preview_id, preview_digest):
        ensure_storage_writable(self.materials.storage_root)
        with self.sessions.begin() as session:
            row = self._row(session, identity, lock=True)
            value = self._preview(row, preview_id)
            if value["preview_digest"] != preview_digest:
                raise MaterialError("PREVIEW_CHANGED", 409)
            if value["state"] == "succeeded":
                return self.materials._view(session, row)
            if value["state"] != "review_required":
                raise MaterialError("MATERIAL_OCR_INCOMPLETE", 409)
            facts = deepcopy(row.parse_facts)
            value["state"] = "succeeded"
            facts["local_ocr"] = value
            facts["issues"] = [issue for issue in facts.get("issues", []) if issue != "OCR_REQUIRES_REVIEW"]
            row.parse_facts = facts
            return self.materials._view(session, row)

    def recover(self, identity, preview_id):
        """Reconcile only after the original kernel-held execution witness is free."""
        ensure_storage_writable(self.materials.storage_root)
        with self.sessions.begin() as session:
            row = self._row(session, identity, lock=True)
            value = self._preview(row, preview_id)
            if value["state"] != "running":
                return self.materials._view(session, row)
            try:
                witness = ExecutorGuard.stopped(self.materials.storage_root, value.get("executor_identity") or {})
            except ExecutorError as error:
                raise MaterialError(str(error), 409) from error
            with witness:
                value["state"] = "partial" if (row.parse_facts or {}).get("ocr") else "failed"
                value["error_code"] = "MATERIAL_OCR_INTERRUPTED"
                value["recovered"] = True
                row.parse_facts = {**row.parse_facts, "local_ocr": value}
                row.error_code = "MATERIAL_OCR_INTERRUPTED"
                row.error_message = "已核实原本地 OCR 执行者停止；保留原件及既有文字，重新处理需要再次预览和确认。"
                row.retryable = True
                return self.materials._view(session, row)

    async def execute(self, identity, preview_id):
        from backend.app.services.paper_import_service import PersonalPaperImportService

        ensure_storage_writable(self.materials.storage_root)
        slot = PersonalPaperImportService._ocr_slot
        if not slot.acquire(blocking=False):
            raise MaterialError("PAPER_OCR_BUSY", 409)
        guard = None
        try:
            with self.sessions.begin() as session:
                row = self._row(session, identity, lock=True)
                value = self._preview(row, preview_id)
                if value["state"] != "confirmed":
                    result = self.materials._view(session, row)
                    slot.release()
                    return result
                snapshot = self._validate(row, value)
                original = deepcopy(row.parse_facts)
                subject_id = row.subject_id
                guard = ExecutorGuard.create(self.materials.storage_root, uuid4())
                value["executor_identity"] = guard.identity
                value["state"] = "running"
                row.parse_facts = {**row.parse_facts, "local_ocr": value}
        except BaseException as error:
            if guard is not None:
                guard.__exit__()
            slot.release()
            if isinstance(error, ExecutorError):
                raise MaterialError(str(error), 409) from error
            raise
        stopped = Event()

        async def work():
            runtime = None
            facts = deepcopy(original)
            sections = []
            error_code = None
            try:
                runtime = self.snapshots.context(snapshot)
                lease = runtime.acquire("ocr")
                for block in ocr_candidates(original):
                    if stopped.is_set():
                        error_code = "OCR_SOFT_TIMEOUT"
                        break
                    path, _ = self.materials.asset_path(subject_id, identity, block["sha256"])
                    result = await lease.client.extract_text(path)
                    if not result.text.strip():
                        error_code = "OCR_EMPTY_RESULT"
                        continue
                    anchor = block["anchor"] + ":ocr"
                    source_index = next(index for index, candidate in enumerate(facts["blocks"])
                                        if candidate.get("type") == "image" and candidate["anchor"] == block["anchor"])
                    facts["blocks"].insert(source_index + 1, {"type": "text", "anchor": anchor, "page": block.get("page"),
                        "text": result.text, "ocr_source_anchor": block["anchor"]})
                    facts.setdefault("ocr", {})[block["anchor"]] = {
                        "snapshot_id": str(snapshot.snapshot_id), "adapter": "rapidocr", "model": value["model"],
                        "regions": [region.model_dump(mode="json") for region in result.regions],
                        "confidence": str(result.confidence) if result.confidence is not None else None,
                    }
                    sections.append(ParsedSection(index=block.get("page") or len(sections) + 1,
                        location=anchor, text=result.text))
            except Exception as error:
                logger.exception("Local material OCR failed")
                error_code = getattr(error, "code", "MATERIAL_OCR_FAILED")
            finally:
                if runtime is not None:
                    try:
                        await runtime.aclose()
                    except Exception:
                        logger.exception("Local material OCR lease cleanup failed")
                        error_code = error_code or "MATERIAL_OCR_RELEASE_FAILED"
            incomplete = bool(ocr_candidates(facts))
            facts["requires_ocr"] = incomplete
            facts["issues"] = [issue for issue in facts.get("issues", []) if issue not in {
                "PDF_REQUIRES_OCR", "DOCX_IMAGE_ONLY_REQUIRES_OCR", "OCR_REQUIRES_REVIEW", "OCR_SOFT_TIMEOUT"}]
            facts["issues"].append("OCR_REQUIRES_REVIEW")
            value["completed_pages"] = len(sections)
            value["error_code"] = error_code
            value["state"] = ("partial" if sections else "failed") if incomplete else "review_required"
            facts["local_ocr"] = value
            with self.sessions.begin() as session:
                row = self._row(session, identity, lock=True)
                current = self._preview(row, preview_id)
                if (current["state"] != "running" or current.get("executor_identity") != value["executor_identity"]
                        or source_digest(row.parse_facts or {}) != value["source_digest"]):
                    raise MaterialError("PREVIEW_CHANGED", 409)
                if sections:
                    parsed = ParsedDocument(file_format=row.file_format, text="\n\n".join(s.text for s in sections),
                        sections=tuple(sections), page_count=facts.get("page_count"))
                    self.materials._write_chunks(session, row, parsed, knowledge_base_id=row.knowledge_base_id)
                facts["chunk_count"] = session.scalar(select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == identity)) or 0
                row.parse_facts = facts
                row.error_code = error_code
                row.error_message = "本地 OCR 未完整完成；原图及已完成文字均已保留。" if error_code else None
                row.retryable = incomplete
            return self.materials.get(identity)

        def worker():
            try:
                return asyncio.run(work())
            finally:
                guard.__exit__()
                slot.release()

        task = asyncio.create_task(asyncio.to_thread(worker))
        self.materials.space.local_ocr_tasks.add(task)
        def finished(done):
            self.materials.space.local_ocr_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.materials.limits.local_wait_seconds)
        except TimeoutError:
            stopped.set()
            return self.materials.get(identity)
        except asyncio.CancelledError:
            stopped.set()
            raise
