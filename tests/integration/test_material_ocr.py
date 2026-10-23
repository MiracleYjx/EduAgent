"""A4 explicit study-material OCR against isolated PostgreSQL and managed files."""

import asyncio
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.app.ai.ingestion.ocr.schemas import OCRResult
from backend.app.models.document import Document
from backend.app.models.document_chunk import DocumentChunk
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import LearningLimits
from backend.app.services.material_indexing import MaterialIndexing
from backend.app.services.material_ocr import MaterialOCR
from tests.contract.test_learning_materials import jpeg_bytes, png_bytes
from tests.integration.test_learning_material_versions import (
    _scanned_pdf_bytes,
    subject,
    upload,
)
from tests.integration.test_learning_material_versions import (
    space as _space,
)

space = _space


class Snapshots:
    def __init__(self, client, *, adapter="rapidocr"):
        self.client = client
        self.revision = uuid4()
        self.adapter = adapter
        self.saved = {}
        self.active_leases = 0

    def create(self, **kwargs):
        assert kwargs["task_kinds"] == ("ocr",)
        value = SimpleNamespace(snapshot_id=uuid4(), settings_revision=self.revision,
            resolved_task_bindings={"ocr": SimpleNamespace(adapter_id=self.adapter, model_id="local-test-ocr")})
        self.saved[value.snapshot_id] = value
        return value

    def load(self, identity):
        return self.saved[identity]

    def context(self, snapshot):
        owner = self
        class Context:
            def acquire(self, kind):
                assert kind == "ocr"
                owner.active_leases += 1
                return SimpleNamespace(client=owner.client)

            async def aclose(self):
                owner.active_leases -= 1
        return Context()


class OCR:
    def __init__(self, *, fail_at=None, gate=None):
        self.calls = []
        self.fail_at = fail_at
        self.gate = gate

    async def extract_text(self, path):
        assert path.is_file()
        self.calls.append(path)
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait)
        if len(self.calls) == self.fail_at:
            raise MaterialError("OCR_CALL_FAILED", 422)
        return OCRResult(text="Actual OCR page " + str(len(self.calls)), confidence=None,
            regions=[{"text": "Actual OCR", "confidence": "0.9", "bbox": (1.0, 1.0, 20.0, 15.0)}])


def service(space, client=None, *, adapter="rapidocr"):
    client = client or OCR()
    snapshots = Snapshots(client, adapter=adapter)
    operations = SimpleNamespace(snapshots=snapshots, config=SimpleNamespace(
        active_revision=lambda: SimpleNamespace(revision_id=snapshots.revision)))
    space.subjects.runtime_snapshots = snapshots
    return MaterialOCR(space.materials, operations), snapshots, client


@pytest.mark.parametrize("name,data", [
    ("scan.pdf", _scanned_pdf_bytes()), ("scan.png", png_bytes()), ("scan.jpeg", jpeg_bytes()),
], ids=["pdf", "png", "jpeg"])
def test_preview_confirm_execute_and_review_preserve_original_and_version(space, name, data):
    sid = subject(space)
    response = upload(space, sid, name, data)
    assert response.status_code == 201, response.text
    original = response.json()
    identity = UUID(original["id"])
    version = space.client.post(f"/api/learning/subjects/{sid}/material-series", json={
        "document_id": str(identity), "series_name": "OCR material", "label": "v1"})
    assert version.status_code == 201, version.text
    ocr, snapshots, client = service(space)
    preview = ocr.prepare(identity, "first")
    assert preview.state == "prepared" and preview.request_budget == 0
    assert client.calls == []
    assert asyncio.run(ocr.execute(identity, preview.id)).ocr.state == "prepared"
    assert client.calls == []
    ocr.confirm(identity, preview.id, preview.preview_digest)
    result = asyncio.run(ocr.execute(identity, preview.id))
    assert result.ocr.state == "review_required" and result.parse_state == "partial"
    assert result.requires_ocr is False and result.chunk_count > 0
    assert len(client.calls) == 1 and snapshots.active_leases == 0
    assert result.assets == space.materials.get(identity).assets
    assert [block.model_dump() for block in result.blocks if block.type == "image"] == original["blocks"]
    assert result.version_label == "v1" and result.version_id is not None
    text = next(block for block in result.blocks if block.type == "text")
    assert text.ocr_source_anchor == original["blocks"][0]["anchor"]
    with space.subjects.sessions() as session:
        with pytest.raises(MaterialError, match="MATERIAL_PARSE_INCOMPLETE"):
            MaterialIndexing(space.materials, ocr.operations)._facts(session, identity)
        row = session.get(Document, identity)
        evidence = row.parse_facts["ocr"][text.ocr_source_anchor]
        assert evidence["regions"][0]["bbox"] == [1.0, 1.0, 20.0, 15.0]
        assert evidence["snapshot_id"] == str(preview.runtime_snapshot_id)
    reviewed = ocr.review(identity, preview.id, preview.preview_digest)
    assert reviewed.ocr.state == "succeeded" and reviewed.parse_state == "parsed"
    assert ocr.review(identity, preview.id, preview.preview_digest) == reviewed
    assert asyncio.run(ocr.execute(identity, preview.id)).chunk_count == reviewed.chunk_count
    assert len(client.calls) == 1


def test_changed_source_or_configuration_cannot_reuse_confirmed_preview(space):
    sid = subject(space)
    identity = UUID(upload(space, sid, "scan.png", png_bytes()).json()["id"])
    ocr, snapshots, client = service(space)
    preview = ocr.prepare(identity, "first")
    with pytest.raises(MaterialError, match="PREVIEW_CHANGED"):
        ocr.confirm(identity, preview.id, "0" * 64)
    snapshots.revision = uuid4()
    with pytest.raises(MaterialError, match="PREVIEW_CHANGED"):
        ocr.confirm(identity, preview.id, preview.preview_digest)
    snapshots.revision = snapshots.load(preview.runtime_snapshot_id).settings_revision
    with space.subjects.sessions.begin() as session:
        row = session.get(Document, identity)
        row.parse_facts = {**row.parse_facts, "issues": ["CHANGED"]}
    with pytest.raises(MaterialError, match="PREVIEW_CHANGED"):
        ocr.confirm(identity, preview.id, preview.preview_digest)
    assert client.calls == []


def test_partial_ocr_keeps_real_text_and_retries_only_remaining_page(space):
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    first, second = Image.new("RGB", (200, 100), "white"), Image.new("RGB", (200, 100), "white")
    first.save(output, format="PDF", save_all=True, append_images=[second])
    first.close()
    second.close()
    sid = subject(space)
    response = upload(space, sid, "two.pdf", output.getvalue())
    assert response.status_code == 201, response.text
    identity = UUID(response.json()["id"])
    ocr, _, client = service(space, OCR(fail_at=2))
    preview = ocr.prepare(identity, "first")
    ocr.confirm(identity, preview.id, preview.preview_digest)
    partial = asyncio.run(ocr.execute(identity, preview.id))
    assert partial.requires_ocr and partial.parse_state == "partial"
    assert partial.ocr.state == "partial" and partial.error_code == "OCR_CALL_FAILED"
    assert partial.chunk_count > 0 and len(client.calls) == 2
    with pytest.raises(MaterialError, match="MATERIAL_OCR_INCOMPLETE"):
        ocr.review(identity, preview.id, preview.preview_digest)
    next_preview = ocr.prepare(identity, "second")
    assert next_preview.page_count == 1
    ocr.confirm(identity, next_preview.id, next_preview.preview_digest)
    complete = asyncio.run(ocr.execute(identity, next_preview.id))
    assert complete.ocr.state == "review_required" and len(client.calls) == 3
    assert len([b for b in complete.blocks if b.ocr_source_anchor]) == 2
    with space.subjects.sessions() as session:
        chunks = list(session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == identity)))
        assert len(chunks) == 2 and {c.chunk_index for c in chunks} == {0, 1}


def test_soft_timeout_keeps_native_lease_and_single_ocr_slot_until_worker_finishes(space):
    sid = subject(space)
    identity = UUID(upload(space, sid, "scan.png", png_bytes()).json()["id"])
    gate = Event()
    ocr, snapshots, client = service(space, OCR(gate=gate))
    space.materials.limits = LearningLimits(local_wait_seconds=0.05)
    preview = ocr.prepare(identity, "first")
    ocr.confirm(identity, preview.id, preview.preview_digest)

    async def scenario():
        try:
            result = await ocr.execute(identity, preview.id)
            assert result.ocr.state == "running" and snapshots.active_leases == 1
            with pytest.raises(MaterialError, match="EXECUTOR_STILL_ACTIVE"):
                ocr.recover(identity, preview.id)
            with pytest.raises(MaterialError, match="PAPER_OCR_BUSY"):
                await ocr.execute(identity, preview.id)
        finally:
            gate.set()
            await asyncio.gather(*tuple(space.subjects.local_ocr_tasks))
        assert snapshots.active_leases == 0 and len(client.calls) == 1
        assert space.materials.get(identity).ocr.state == "review_required"
    asyncio.run(scenario())


def test_remote_ocr_and_paper_purpose_never_reach_a_provider(space):
    sid = subject(space)
    identity = UUID(upload(space, sid, "scan.png", png_bytes()).json()["id"])
    ocr, _, client = service(space, adapter="vision")
    with pytest.raises(MaterialError, match="LOCAL_OCR_REQUIRED"):
        ocr.prepare(identity, "remote")
    paper = UUID(upload(space, sid, "scan.png", png_bytes(), purpose="exam_paper").json()["id"])
    with pytest.raises(MaterialError, match="MATERIAL_NOT_STUDY_MATERIAL"):
        ocr.prepare(paper, "paper")
    assert client.calls == []


def test_cancelled_wait_keeps_native_lease_until_real_completion(space):
    sid = subject(space)
    identity = UUID(upload(space, sid, "scan.png", png_bytes()).json()["id"])
    gate = Event()
    ocr, snapshots, client = service(space, OCR(gate=gate))
    preview = ocr.prepare(identity, "cancel")
    ocr.confirm(identity, preview.id, preview.preview_digest)

    async def scenario():
        waiter = asyncio.create_task(ocr.execute(identity, preview.id))
        try:
            for _ in range(100):
                if client.calls:
                    break
                await asyncio.sleep(0.01)
            assert client.calls
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert snapshots.active_leases == 1
            with pytest.raises(MaterialError, match="PAPER_OCR_BUSY"):
                await ocr.execute(identity, preview.id)
        finally:
            gate.set()
            await asyncio.gather(*tuple(space.subjects.local_ocr_tasks))
        assert snapshots.active_leases == 0 and len(client.calls) == 1
    asyncio.run(scenario())


def test_http_routes_freeze_and_review_without_network_or_implicit_execution(space):
    from backend.app.api.learning_operations import service as operation_service

    sid = subject(space)
    identity = upload(space, sid, "scan.png", png_bytes()).json()["id"]
    ocr, _, client = service(space)
    space.client.app.dependency_overrides[operation_service] = lambda: ocr.operations
    base = f"/api/learning/materials/{identity}"
    response = space.client.post(base + "/ocr-preview", json={"idempotency_key": "http"})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert client.calls == []
    path = base + "/ocr/" + preview["id"]
    execute_before = space.client.post(path + "/execute", json={})
    assert execute_before.status_code == 200 and execute_before.json()["ocr"]["state"] == "prepared"
    assert client.calls == []
    confirmed = space.client.post(path + "/confirm", json={"preview_digest": preview["preview_digest"]})
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "confirmed"
    executed = space.client.post(path + "/execute", json={})
    assert executed.status_code == 200, executed.text
    assert executed.json()["ocr"]["state"] == "review_required"
    reviewed = space.client.post(path + "/review", json={"preview_digest": preview["preview_digest"]})
    assert reviewed.status_code == 200 and reviewed.json()["parse_state"] == "parsed"
    assert len(client.calls) == 1


def test_interrupted_local_ocr_requires_original_stopped_witness_and_new_confirmation(space):
    from backend.app.services.operation_executor import ExecutorGuard

    sid = subject(space)
    identity = UUID(upload(space, sid, "scan.png", png_bytes()).json()["id"])
    ocr, _, client = service(space)
    preview = ocr.prepare(identity, "first")
    ocr.confirm(identity, preview.id, preview.preview_digest)
    guard = ExecutorGuard.create(space.materials.storage_root, uuid4())
    with space.subjects.sessions.begin() as session:
        row = session.get(Document, identity)
        value = {**row.parse_facts["local_ocr"], "state": "running", "executor_identity": guard.identity}
        row.parse_facts = {**row.parse_facts, "local_ocr": value}
    try:
        with pytest.raises(MaterialError, match="EXECUTOR_STILL_ACTIVE"):
            ocr.recover(identity, preview.id)
    finally:
        guard.__exit__()
    recovered = ocr.recover(identity, preview.id)
    assert recovered.ocr.state == "failed" and recovered.ocr.error_code == "MATERIAL_OCR_INTERRUPTED"
    assert client.calls == [] and recovered.requires_ocr
    fresh = ocr.prepare(identity, "new-user-preview")
    assert fresh.id != preview.id and fresh.state == "prepared"
    assert asyncio.run(ocr.execute(identity, fresh.id)).ocr.state == "prepared"
    assert client.calls == []


def test_http_uses_subject_owned_snapshot_for_actual_local_ocr_outside_paid_invocation(space, tmp_path):
    """Real provider construction reproduces the frozen-artifact wiring boundary."""
    import os
    from io import BytesIO
    from pathlib import Path

    from PIL import Image, ImageDraw, ImageFont

    from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
    from tests.support.personal_paper_runtime import configured_paper_runtime

    sid = subject(space)
    image = Image.new("RGB", (700, 180), "white")
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default(size=36)
    ImageDraw.Draw(image).text((20, 40), "ADDITION 2 + 2 = 4", fill="black", font=font)
    data = BytesIO()
    image.save(data, format="PNG")
    image.close()
    response = upload(space, sid, "real-assembly.png", data.getvalue())
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    model_dir = Path(os.environ["EDUAGENT_TEST_OCR_MODEL_DIR"])
    harness, operations, calls = asyncio.run(configured_paper_runtime(tmp_path / "runtime",
        space.profile.environment_id, model_dir, space.subjects.sessions))
    space.subjects.config = harness.service
    space.subjects.runtime_snapshots = RuntimeSnapshotService(harness.service)
    # This is the production LearningOperations class, with its guarded
    # Invocation-only factory installed. OCR must never acquire through it.
    space.client.app.state.learning_operations = operations
    try:
        base = f"/api/learning/materials/{identity}"
        preview = space.client.post(base + "/ocr-preview", json={"idempotency_key": "real-assembly"})
        assert preview.status_code == 200, preview.text
        value = preview.json()
        path = base + "/ocr/" + value["id"]
        assert space.client.post(path + "/confirm", json={"preview_digest": value["preview_digest"]}).status_code == 200
        executed = space.client.post(path + "/execute", json={})
        assert executed.status_code == 200, executed.text
        result = executed.json()
        assert result["ocr"]["state"] == "review_required", result
        text = " ".join(block.get("text") or "" for block in result["blocks"])
        assert "addition" in text.casefold()
        assert result["chunk_count"] > 0 and result["assets"]
        assert calls == [] and operations.snapshots.active_lease_count == 0
        assert space.subjects.runtime_snapshots.active_lease_count == 0
    finally:
        asyncio.run(space.subjects.runtime_snapshots.aclose())
        asyncio.run(operations.aclose())
        space.client.app.state.learning_operations = None
        space.subjects.runtime_snapshots = None
        harness.close()
