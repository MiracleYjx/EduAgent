"""Explicit material index publication using the existing durable operation ledger."""

import asyncio
from uuid import UUID

from pydantic import BaseModel, FiniteFloat
from sqlalchemy import select

from backend.app.core.maintenance import ensure_storage_writable
from backend.app.domain.enums import DocumentPurpose
from backend.app.models.document import Document
from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.knowledge_base import KnowledgeBase
from backend.app.models.operation_run import OperationRun
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import OperationError, StepPlan, digest
from backend.app.services.settings.index_identity import normalize_identity

LOCAL_EMBEDDING = frozenset({"local", "bge", "huggingface"})


class IndexedVector(BaseModel):
    vector: list[FiniteFloat]
    identity: dict


def material_digest(row, chunks):
    return digest({"document_id":str(row.id),"subject_id":str(row.subject_id),
        "knowledge_base_id":str(row.knowledge_base_id),"file_metadata":row.file_metadata,
        "parse_facts":{k:v for k,v in (row.parse_facts or {}).items() if not k.startswith("index_")},
        "chunks":[{"id":str(c.id),"content":c.content,"metadata":c.chunk_metadata,
            "chapter_id":str(c.chapter_id) if c.chapter_id else None} for c in chunks]})


class MaterialIndexing:
    def __init__(self, materials, operations):
        self.materials = materials
        self.operations = operations
        self.sessions = materials.sessions

    def _facts(self, session, identity, *, lock=False):
        self.materials.space._check_space()
        row = session.get(Document, identity, with_for_update=lock)
        if row is None or row.subject_id is None:
            raise MaterialError("MATERIAL_NOT_FOUND", 404)
        self.materials._subject(session, row.subject_id)
        if row.purpose != DocumentPurpose.KNOWLEDGE_BASE:
            raise MaterialError("MATERIAL_NOT_STUDY_MATERIAL", 409)
        facts = row.parse_facts or {}
        if (row.material_state not in {"parsed", "indexed"} or facts.get("issues")
                or facts.get("requires_ocr") or facts.get("unprocessed_pages")):
            raise MaterialError("MATERIAL_PARSE_INCOMPLETE", 409)
        chunks = list(session.scalars(select(DocumentChunk).where(
            DocumentChunk.document_id == row.id).order_by(DocumentChunk.chunk_index, DocumentChunk.id)))
        if not chunks:
            raise MaterialError("MATERIAL_HAS_NO_CHUNKS", 409)
        kb = session.get(KnowledgeBase, row.knowledge_base_id)
        if kb is None or kb.subject_id != row.subject_id:
            raise MaterialError("MATERIAL_KNOWLEDGE_BASE_MISSING", 409)
        return row, chunks, material_digest(row, chunks)

    def _validate(self, identity, operation, *, allow_published=False):
        if operation.purpose != "material_index:" + str(identity):
            raise OperationError("MATERIAL_OPERATION_MISMATCH")
        with self.sessions() as session:
            row, _chunks, current = self._facts(session, identity)
            if (row.parse_facts or {}).get("index_operation_id") != str(operation.id):
                raise OperationError("PREVIEW_CHANGED")
            if row.material_state == "indexed" and allow_published:
                return
            if any(s.get("material_input", {}).get("digest") != current for s in operation.steps):
                raise OperationError("PREVIEW_CHANGED")
        active = self.operations.config.active_revision()
        snapshot = self.operations.snapshots.load(operation.runtime_snapshot_id)
        if operation.state in {"prepared", "confirmed"} and (active is None or active.revision_id != snapshot.settings_revision):
            raise OperationError("PREVIEW_CHANGED")

    async def prepare(self, identity, idempotency_key):
        ensure_storage_writable(self.materials.storage_root)
        with self.sessions() as session:
            row, chunks, frozen = self._facts(session, identity)
            previous = (row.parse_facts or {}).get("index_operation_id")
            texts = [(str(c.id), c.content) for c in chunks]
            if row.material_state == "indexed":
                raise MaterialError("MATERIAL_ALREADY_INDEXED", 409)
        if previous:
            old = self.operations.view(UUID(previous))
            if old.state in {"running", "outcome_unknown"} or (old.state == "succeeded"
                    and all(s.get("material_input", {}).get("digest") == frozen for s in old.steps)):
                return old
        active = self.operations.config.active_revision()
        if active is None:
            raise OperationError("OPERATION_DEPENDENCY_UNAVAILABLE")
        registry = self.operations.config.provider_registry
        binding = registry.resolve_binding(task_kind="embedding", profiles=active.values.providers,
            bindings=active.values.bindings)
        profile = active.values.providers[str(binding.profile_id)]
        client = registry.create(profile, kind="embedding", credential_store=self.operations.config.credential_store)
        try:
            candidate = normalize_identity(client.index_identity())
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        if candidate["dimension"] != 1024:
            raise MaterialError("EMBEDDING_DIMENSION_MISMATCH", 409)
        local = profile.adapter_id in LOCAL_EMBEDDING
        with self.sessions() as session:
            row, _, _ = self._facts(session, identity)
            kb = session.get(KnowledgeBase, row.knowledge_base_id)
            if kb.embedding_identity is not None:
                from backend.app.services.settings.index_identity import (
                    IndexIdentityService,
                    SqlAlchemyIndexRepository,
                )
                IndexIdentityService(repository=SqlAlchemyIndexRepository(session)).require_compatible(kb_id=kb.id, identity=candidate)
        key = "material-index:" + digest({"document":str(identity),"key":idempotency_key})
        reusable = previous and old.state in {"failed", "partial"} and all(
            s.get("material_input", {}).get("digest") == frozen
            and s.get("material_input", {}).get("identity") == candidate for s in old.steps)
        if reusable and self.operations.snapshots.load(old.runtime_snapshot_id).settings_revision == active.revision_id:
            operation = self.operations.retry(old.id, idempotency_key=key,
                storage_root=self.materials.storage_root, expected_settings_revision=active.revision_id)
        else:
            operation = self.operations.prepare(purpose="material_index:" + str(identity),
            idempotency_key=key,
            steps=[StepPlan(step_id="chunk-"+chunk_id, task_kind="embedding", messages=[],
                embedding_inputs=[content], request_budget=0 if local else 1, local_only=local,
                material_input={"document_id":str(identity),"chunk_id":chunk_id,"digest":frozen,"identity":candidate},
                scope_summary=("本地Embedding；0次HTTP发送，无远程模型费用。" if local else
                    "远程Embedding；此片段最多1次请求，费用无法可靠估算，预算不是供应商账单上限。"))
                for chunk_id, content in texts], storage_root=self.materials.storage_root,
            expected_settings_revision=active.revision_id)
        with self.sessions.begin() as session:
            row, _, current = self._facts(session, identity, lock=True)
            if current != frozen:
                raise OperationError("PREVIEW_CHANGED")
            latest = (row.parse_facts or {}).get("index_operation_id")
            if latest != previous:
                raise OperationError("PREVIEW_CHANGED")
            if latest:
                old = session.get(OperationRun, UUID(latest), with_for_update=True)
                if old.state in {"running", "outcome_unknown"} or (old.state == "succeeded"
                        and all(s.get("material_input", {}).get("digest") == current for s in old.steps)):
                    return self.operations.view(old.id)
            row.parse_facts = {**row.parse_facts, "index_operation_id":str(operation.id)}
        return operation

    def confirm(self, identity, operation_id, preview_digest):
        operation = self.operations.view(operation_id)
        self._validate(identity, operation)
        return self.operations.confirm(operation_id, preview_digest=preview_digest)

    async def execute(self, identity, operation_id):
        ensure_storage_writable(self.materials.storage_root)
        operation = self.operations.view(operation_id)
        self._validate(identity, operation, allow_published=True)
        if self.materials.get(identity).state == "indexed":
            return self.materials.get(identity)

        async def invoke(runtime, step):
            lease = runtime.acquire("embedding")
            client = lease.client
            actual_identity = normalize_identity(client.index_identity())
            if actual_identity != step.material_input["identity"]:
                raise OperationError("PREVIEW_CHANGED")
            work = asyncio.create_task(client.embed_documents(step.embedding_inputs))
            if step.local_only:
                cancelled = False
                while True:
                    try:
                        vectors = await asyncio.shield(work)
                        break
                    except asyncio.CancelledError:
                        # Repeated cancellation must never propagate into the
                        # native worker or release its durable model slot early.
                        cancelled = True
                        if work.done():
                            break
                if cancelled:
                    raise asyncio.CancelledError
            else:
                vectors = await work
            if len(vectors) != 1:
                raise MaterialError("EMBEDDING_COUNT_MISMATCH", 409)
            return IndexedVector(vector=vectors[0], identity=actual_identity)

        def before_claim(session):
            row, _, current = self._facts(session, identity, lock=True)
            if ((row.parse_facts or {}).get("index_operation_id") != str(operation.id)
                    or any(s["material_input"]["digest"] != current for s in operation.steps)):
                raise OperationError("PREVIEW_CHANGED")
            active = self.operations.config.active_revision()
            snapshot = self.operations.snapshots.load(operation.runtime_snapshot_id)
            if active is None or active.revision_id != snapshot.settings_revision:
                raise OperationError("PREVIEW_CHANGED")

        operation = await self.operations.execute(operation_id, invoke=invoke, before_claim=before_claim)
        if operation.state != "succeeded":
            return operation
        self._validate(identity, operation)
        # Successful immutable chunks from an explicit retry's ancestors are
        # reused locally. Only the retry's remaining steps were sent again.
        saved = {}
        chain = []
        parent = operation
        visited = set()
        while parent is not None:
            if parent.id in visited:
                raise OperationError("MATERIAL_OPERATION_MISMATCH")
            visited.add(parent.id)
            chain.append(parent)
            parent = self.operations.view(parent.retry_of_id) if parent.retry_of_id else None
        frozen = operation.steps[0]["material_input"]["digest"]
        snapshot = self.operations.snapshots.load(operation.runtime_snapshot_id)
        for ancestor in reversed(chain):
            if ancestor.purpose != operation.purpose or self.operations.snapshots.load(ancestor.runtime_snapshot_id).settings_revision != snapshot.settings_revision:
                raise OperationError("PREVIEW_CHANGED")
            for step in ancestor.steps:
                if step["material_input"]["digest"] != frozen:
                    raise OperationError("PREVIEW_CHANGED")
                if step["state"] == "succeeded":
                    saved[step["material_input"]["chunk_id"]] = IndexedVector.model_validate(step["result"])
        with self.sessions() as session:
            _, chunks, _ = self._facts(session, identity)
            if set(saved) != {str(c.id) for c in chunks}:
                raise MaterialError("MATERIAL_INDEX_INCOMPLETE", 409)
            results = [saved[str(c.id)] for c in chunks]
        if any(result.identity != results[0].identity for result in results):
            raise OperationError("PREVIEW_CHANGED")
        return self.materials.index_material(identity, identity=results[0].identity,
            vectors=[r.vector for r in results], operation_id=operation_id,
            expected_digest=operation.steps[0]["material_input"]["digest"])
