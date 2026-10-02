"""T164/TCR §17: authorized image identity and source-pixel mapping boundaries."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.schemas.paper_import import PixelRegion
from backend.app.services.image_understanding_service import _map_region


def image(*, page=None, region=None):
    return SimpleNamespace(asset_id=uuid4(), image_index=1, width=80, height=40,
        source_page_id=page, region=region)


def test_source_pixels_use_actual_crop_offset_and_unknown_mapping_stays_unknown():
    box = PixelRegion(bbox=(3, 4, 20, 30))
    assert _map_region(box, image()) == box
    assert _map_region(box, image(page=uuid4())) is None
    mapped = _map_region(box, image(page=uuid4(), region=PixelRegion(bbox=(100, 200, 180, 240))))
    assert mapped is not None and mapped.bbox == (103, 204, 120, 230)
    assert _map_region(box, image(page=uuid4(), region=PixelRegion(bbox=(100, 200, 181, 240)))) is None


def test_model_box_outside_the_actual_input_image_is_rejected():
    with pytest.raises(ValueError, match="实际题图"):
        _map_region(PixelRegion(bbox=(1, 1, 81, 30)), image())

# Real SQLite ownership and persistent bytes; machine responses are declared test doubles.
import asyncio

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages
from backend.app.models import ExtractedQuestion
from backend.app.schemas.image_assessment import ImageAssessment
from backend.app.schemas.paper_import import CorrectionPayload
from backend.app.services.content_validation_service import ContentValidationError
from backend.app.services.image_understanding_service import ImageUnderstandingService
from backend.app.services.question_correction_service import QuestionCorrectionService
from tests.unit.services.test_content_validation_service import image_case
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)
from tests.unit.settings_helpers import build_test_settings

owned_document = _owned_document


class _LifecycleVisionStub(BaseLLMProvider):
    provider_name = "stub"

    def __init__(self, *, supported=True, cancel_call=False, close_error=None, on_call=None, on_close=None):
        self.model_name = "fixture-vision"
        self.supported = supported
        self.cancel_call = cancel_call
        self.close_error = close_error
        self.on_call = on_call
        self.on_close = on_close
        self.calls = 0
        self.closed = 0

    def supports_vision(self):
        return self.supported

    async def generate_structured(self, messages: LLMMessages, schema: type[BaseModel], model: str | None = None):
        self.calls += 1
        assert model is None
        assert messages[-1]["content"][-1]["type"] == "image"
        if self.on_call is not None:
            self.on_call()
        if self.cancel_call:
            raise asyncio.CancelledError()
        return schema.model_validate({
            "observations": [{"image_index": 1, "kind": "fixture diagram", "description": "synthetic crop", "bbox": [2, 3, 10, 12]}],
            "conditions": [{"image_index": 1, "text": "Declared test condition", "evidence_region": {"bbox": [2, 3, 10, 12]}}],
            "unresolved_issues": [],
            "requires_manual_review": False,
            "provenance": None,
        })

    async def aclose(self):
        self.closed += 1
        if self.on_close is not None:
            self.on_close()
        if self.close_error is not None:
            raise self.close_error


def _case(owned):
    session, _doc, teacher, root = owned
    persistence, extracted, asset = image_case(owned)
    service = ImageUnderstandingService(session, root=root, settings=build_test_settings(vision_model=None))
    return service, persistence, extracted, asset, teacher


def _use_stub(monkeypatch, provider):
    monkeypatch.setattr("backend.app.services.image_understanding_service.create_vision_provider", lambda _settings: provider)


def _run(service, extracted, teacher):
    return asyncio.run(service.understand("extracted_question", extracted.id, actor_id=teacher.id))


def _saved(owned, extracted):
    session = owned[0]
    session.expire_all()
    return ImageAssessment.model_validate(session.get(ExtractedQuestion, extracted.id).image_assessment)


def test_missing_configuration_is_durable_technical_error_without_provider_identity(owned_document):
    service, _persistence, extracted, _asset, teacher = _case(owned_document)
    view = _run(service, extracted, teacher)
    assert view.current_run.outcome == "technical_error"
    assert view.current_run.error.code == "VISION_PROVIDER_NOT_READY"
    assert view.current_run.provenance.provider_name is None
    assert view.current_run.provenance.model is None
    assert view.current_run.requested_by == teacher.id
    assert view.status == "pending" and not view.confirmed_conditions
    run = _saved(owned_document, extracted).runs[0]
    assert run.completed_at is not None and run.completed_at >= run.started_at
    assert run.result is None


def test_unsupported_provider_never_calls_model_and_is_closed_once(owned_document, monkeypatch):
    service, _persistence, extracted, _asset, teacher = _case(owned_document)
    provider = _LifecycleVisionStub(supported=False)
    _use_stub(monkeypatch, provider)
    view = _run(service, extracted, teacher)
    assert provider.calls == 0 and provider.closed == 1
    assert view.current_run.error.code == "VISION_NOT_SUPPORTED"
    assert view.current_run.provenance.model is None
    assert _saved(owned_document, extracted).runs[0].outcome == "technical_error"


def test_fake_machine_success_binds_authorized_asset_and_keeps_teacher_pending(owned_document, monkeypatch):
    service, _persistence, extracted, asset, teacher = _case(owned_document)
    provider = _LifecycleVisionStub()
    _use_stub(monkeypatch, provider)
    view = _run(service, extracted, teacher)
    assert provider.calls == 1 and provider.closed == 1
    assert view.current_run.outcome == "completed"
    assert view.current_run.result.conditions[0].asset_id == asset.id
    assert view.current_run.result.conditions[0].evidence_region.bbox == (12, 13, 20, 22)
    assert view.current_run.provenance.provider_name == "stub"
    assert view.current_run.provenance.model == "fixture-vision"
    assert view.status == "pending" and view.current_check is None
    assert not view.confirmed_conditions and view.requires_manual_review
    assert _saved(owned_document, extracted).manual_checks == []


def test_actual_call_cancellation_is_saved_with_actual_stub_source_then_rethrown(owned_document, monkeypatch):
    service, _persistence, extracted, _asset, teacher = _case(owned_document)
    provider = _LifecycleVisionStub(cancel_call=True)
    _use_stub(monkeypatch, provider)
    with pytest.raises(asyncio.CancelledError):
        _run(service, extracted, teacher)
    saved = _saved(owned_document, extracted).runs[0]
    assert provider.calls == 1 and provider.closed == 1
    assert saved.outcome == "technical_error" and saved.result is None
    assert saved.error.code == "VISION_CALL_FAILED" and saved.error.cause == "CancelledError"
    assert saved.provenance.provider_name == "stub"
    assert saved.completed_at is not None


def test_close_exception_preserves_machine_completion_and_propagates_cleanup_failure(owned_document, monkeypatch):
    service, _persistence, extracted, _asset, teacher = _case(owned_document)
    provider = _LifecycleVisionStub(close_error=RuntimeError("fixture close failure"))
    _use_stub(monkeypatch, provider)
    with pytest.raises(RuntimeError, match="fixture close failure"):
        _run(service, extracted, teacher)
    run = _saved(owned_document, extracted).runs[0]
    assert provider.calls == 1 and provider.closed == 1
    assert run.outcome == "completed" and run.result is not None
    assert run.error is None


def test_close_cancellation_preserves_already_received_result_then_rethrows_cancel(owned_document, monkeypatch):
    service, _persistence, extracted, _asset, teacher = _case(owned_document)
    provider = _LifecycleVisionStub(close_error=asyncio.CancelledError())
    _use_stub(monkeypatch, provider)
    with pytest.raises(asyncio.CancelledError):
        _run(service, extracted, teacher)
    run = _saved(owned_document, extracted).runs[0]
    assert provider.calls == 1 and provider.closed == 1
    assert run.outcome == "completed" and run.result is not None
    assert run.completed_at is not None and run.error is None


def test_finish_commit_failure_propagates_once_with_saved_running_state(owned_document, monkeypatch):
    service, _persistence, extracted, _asset, teacher = _case(owned_document)
    session = owned_document[0]
    actual_commit = session.commit
    finish = service.persistence.finish_image_run
    finishes = []

    def record_finish(*args, **kwargs):
        finishes.append(kwargs)
        return finish(*args, **kwargs)

    def fail_commit():
        raise SQLAlchemyError("fixture database commit failure")

    provider = _LifecycleVisionStub(on_close=lambda: monkeypatch.setattr(session, "commit", fail_commit))
    _use_stub(monkeypatch, provider)
    monkeypatch.setattr(service.persistence, "finish_image_run", record_finish)
    try:
        with pytest.raises(ContentValidationError) as failure:
            _run(service, extracted, teacher)
        assert failure.value.code == "CONTENT_VALIDATION_PERSISTENCE_FAILED"
    finally:
        monkeypatch.setattr(session, "commit", actual_commit)
    assert len(finishes) == 1 and finishes[0]["error"] is None
    assert provider.calls == 1 and provider.closed == 1
    assert _saved(owned_document, extracted).runs[0].outcome == "running"


def test_late_result_after_real_correction_keeps_history_without_confirming_new_context(owned_document, monkeypatch):
    service, _persistence, extracted, _asset, teacher = _case(owned_document)
    session, _doc, _teacher, root = owned_document

    def real_correction():
        QuestionCorrectionService(session, root=root).patch(
            extracted.paper_import_id, extracted.id,
            CorrectionPayload(content="Changed real teacher input while model was running"),
            actor_id=teacher.id,
        )

    provider = _LifecycleVisionStub(on_call=real_correction)
    _use_stub(monkeypatch, provider)
    view = _run(service, extracted, teacher)
    saved = _saved(owned_document, extracted)
    assert provider.calls == 1 and provider.closed == 1
    assert saved.context_revision == 1
    assert saved.runs[0].context_revision == 0 and saved.runs[0].outcome == "completed"
    assert view.current_run is None and view.status == "pending"
    assert not view.confirmed_conditions and not saved.manual_checks


def test_missing_authorized_file_keeps_original_code_and_never_creates_provider(owned_document, monkeypatch):
    service, persistence, extracted, asset, teacher = _case(owned_document)
    path, _ = persistence.files.download(asset.file_id, actor_id=teacher.id)
    path.unlink()

    def no_provider(_settings):
        raise AssertionError("文件丢失时不得构造模型调用")

    monkeypatch.setattr("backend.app.services.image_understanding_service.create_vision_provider", no_provider)
    view = _run(service, extracted, teacher)
    saved = _saved(owned_document, extracted).runs[0]
    assert saved.outcome == "technical_error"
    assert saved.error.code == "FILE_MISSING" and saved.error.stage == "file"
    assert saved.provenance.model is None and saved.result is None
    assert view.status == "pending"


def test_changed_input_before_prepare_keeps_state_error_stage_and_has_no_model_identity(owned_document, monkeypatch):
    service, _persistence, extracted, _asset, teacher = _case(owned_document)
    session, _doc, _teacher, root = owned_document
    prepare = service.persistence.prepare_image_run

    def change_before_prepare(*args, **kwargs):
        QuestionCorrectionService(session, root=root).patch(
            extracted.paper_import_id, extracted.id,
            CorrectionPayload(content="Real input changed before image preparation"),
            actor_id=teacher.id,
        )
        return prepare(*args, **kwargs)

    def no_provider(_settings):
        raise AssertionError("输入轮次过期时不能创建图像调用")

    monkeypatch.setattr(service.persistence, "prepare_image_run", change_before_prepare)
    monkeypatch.setattr("backend.app.services.image_understanding_service.create_vision_provider", no_provider)
    view = _run(service, extracted, teacher)
    saved = _saved(owned_document, extracted)
    run = saved.runs[0]
    assert run.outcome == "technical_error" and run.result is None
    assert run.error.code == "IMAGE_ASSESSMENT_STALE" and run.error.stage == "input"
    assert run.provenance.provider_name is None and run.provenance.model is None
    assert run.context_revision == 0 and saved.context_revision == 1
    assert view.current_run is None and view.status == "pending"
