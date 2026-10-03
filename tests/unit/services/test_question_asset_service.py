"""T154 actual image bytes, source relationships, lifecycle. TCR §10."""
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from backend.app.models import ExtractedQuestion, Question
from backend.app.schemas.image_assessment import ImageManualCheckRequest
from backend.app.schemas.question_assets import AssetLinkRequest
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.question_asset_service import QuestionAssetService
from backend.app.services.question_service import QuestionService
from tests.support.question_validation_fixtures import persist_current_semantic_pass
from tests.unit.models.test_paper_import_models import paper
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)
from tests.unit.settings_helpers import build_test_settings

owned_document = _owned_document


def png():
    image = Image.new("RGB", (120, 80), "white")
    image.putpixel((20, 20), (0, 0, 0))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def staged_case(owned):
    session, doc, teacher, root = owned
    imported = paper(session, doc, teacher)
    imported.status = "Parsing"
    session.commit()
    service = QuestionAssetService(session, root=root)
    page = service.create_source_page(imported.id, page_number=1, content=png(), actor_id=teacher.id)
    extracted = ExtractedQuestion(
        paper_import_id=imported.id, source_page_ids=[str(page.id)], extracted_by="TEXT",
        status="Pending Correction", assets=[],
    )
    imported.status = "Pending Review"
    session.add(extracted)
    session.commit()
    return service, page, extracted


def test_staged_crop_identity_bytes_and_source_restart(owned_document):
    session, _doc, teacher, root = owned_document
    service, page, extracted = staged_case(owned_document)
    created = service.create_staged(extracted.id, AssetLinkRequest(
        file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="diagram",
        region={"bbox": [10, 10, 40, 30]},
    ), actor_id=teacher.id)
    assert created.id is not None and created.file_id == "a_" + created.id.hex
    assert "file_meta" not in created.model_dump()
    assert session.get(Question, created.id) is None
    files = FileStorageService(session, root=root)
    path, view = files.download(created.file_id, actor_id=teacher.id)
    assert view.resource_type == "staged_asset"
    with Image.open(path) as cropped:
        assert cropped.size == (30, 20)
        assert cropped.getpixel((10, 10)) == (0, 0, 0)
    session.expire_all()
    saved = session.get(ExtractedQuestion, extracted.id)
    assert saved.assets[0]["id"] == str(created.id)
    assert saved.assets[0]["source_page_id"] == str(page.id)
    assert saved.assets[0]["file_meta"]["storage_path"].startswith("assets/")
    assert service.list_staged(saved.id, actor_id=teacher.id)[0].file_id == created.file_id


def test_same_import_max_five_and_full_array_replacement(owned_document):
    _session, _doc, teacher, _root = owned_document
    service, page, extracted = staged_case(owned_document)
    request = AssetLinkRequest(file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="figure")
    identities = [service.create_staged(extracted.id, request, actor_id=teacher.id).id for _ in range(5)]
    with pytest.raises(FileStorageError) as error:
        service.create_staged(extracted.id, request, actor_id=teacher.id)
    assert error.value.code == "QUESTION_ASSET_LIMIT"
    assert len(extracted.assets) == 5
    service.remove_staged(extracted.id, identities[0], actor_id=teacher.id)
    assert len(extracted.assets) == 4
    with pytest.raises(FileStorageError) as error:
        service.create_staged(extracted.id, request.model_copy(update={"source_page_id": uuid4()}), actor_id=teacher.id)
    assert error.value.code == "FILE_REFERENCE_CONFLICT"
    assert page.image_path


def test_invalid_crop_does_not_write_and_terminal_staging_is_immutable(owned_document):
    session, _doc, teacher, root = owned_document
    service, page, extracted = staged_case(owned_document)
    with pytest.raises(FileStorageError):
        service.create_staged(extracted.id, AssetLinkRequest(file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="table", region={"bbox": [0, 0, 121, 80]}), actor_id=teacher.id)
    assert not (root / "assets").exists()
    extracted.status = "Rejected"
    extracted.correction_notes = "教师拒绝"
    session.commit()
    with pytest.raises(FileStorageError) as error:
        service.create_staged(extracted.id, AssetLinkRequest(file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="table"), actor_id=teacher.id)
    assert error.value.code == "PAPER_STATE_CONFLICT"


def test_formal_asset_approved_guard_and_no_physical_unlink(
    owned_document, monkeypatch
):
    session, doc, teacher, root = owned_document
    service, page, _extracted = staged_case(owned_document)
    settings = build_test_settings(storage_root=root)
    monkeypatch.setattr(
        "backend.app.services.file_storage_service.get_settings", lambda: settings
    )
    questions = QuestionService(session)
    summary = questions.create_question(
        doc.course_id,
        "SHORT_ANSWER",
        "含图问题",
        created_by=teacher.id,
        reference_answer="裁剪图中有一个黑色像素点。",
        scoring_rubric="说明唯一黑色像素点计1分。",
        score="1.00",
    )
    asset = service.link_question(
        summary.id,
        AssetLinkRequest(
            file_id="p_" + page.id.hex,
            source_page_id=page.id,
            asset_type="figure",
            region={"bbox": [10, 10, 40, 30]},
        ),
        actor_id=teacher.id,
    )
    questions.update_question_status(
        summary.id, "Pending Review", teacher_id=teacher.id
    )
    content = ContentValidationService(session, root=root)
    view = content.get_image_assessment(
        "question", asset.question_id, actor_id=teacher.id
    )
    assert view.evidence_readable
    checked = content.manual_image_check(
        "question",
        asset.question_id,
        ImageManualCheckRequest(
            expected_context_revision=view.context_revision,
            expected_run_no=view.run_no,
            expected_check_no=view.check_no,
            status="confirmed",
            image_findings=[
                {
                    "asset_id": asset.id,
                    "finding": "conditions_confirmed",
                    "reason": "受控夹具对照实际裁剪图确认一个黑色像素点。",
                }
            ],
            confirmed_conditions=[
                {
                    "asset_id": asset.id,
                    "text": "裁剪图中有一个黑色像素点。",
                    "evidence_region": {"bbox": [10, 10, 11, 11]},
                }
            ],
            explanation="受控角色实际读取原图核对，用于生命周期回归；不是独立教师质量标注。",
        ),
        actor_id=teacher.id,
    )
    assert checked.status == "confirmed"
    persist_current_semantic_pass(session, summary.id, teacher.id, root=root)
    questions.update_question_status(summary.id, "Approved", teacher_id=teacher.id)
    with pytest.raises(FileStorageError) as error:
        service.remove_question(summary.id, asset.id, actor_id=teacher.id)
    assert error.value.code == "QUESTION_APPROVED_IMMUTABLE"
    questions.update_question_status(
        summary.id,
        "Needs Revision",
        teacher_id=teacher.id,
        revision_comment="受控教师记录：修订当前题图关联。",
    )
    path, _ = FileStorageService(session, root=root).download(
        asset.file_id, actor_id=teacher.id
    )
    service.remove_question(summary.id, asset.id, actor_id=teacher.id)
    assert path.is_file()


def test_staged_removal_keeps_bytes_and_history_reference_protection(owned_document):
    from copy import deepcopy
    from datetime import UTC, datetime
    session, _doc, teacher, root = owned_document
    service, page, extracted = staged_case(owned_document)
    staged = service.create_staged(extracted.id, AssetLinkRequest(file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="figure", region={"bbox": [10, 10, 40, 30]}), actor_id=teacher.id)
    files = FileStorageService(session, root=root)
    path, _view = files.download(staged.file_id, actor_id=teacher.id)
    locator = extracted.assets[0]["file_meta"]["storage_path"]
    now = datetime.now(UTC).isoformat()
    assessment = deepcopy(extracted.image_assessment)
    assessment["runs"] = [{
        "id": str(uuid4()), "run_no": 1, "context_revision": 0, "task": "原图条件",
        "input_refs": {"text_fields": ["content"], "images": [{"asset_id": str(staged.id), "file_id": staged.file_id, "image_index": 1, "asset_type": "figure", "source_page_id": str(page.id), "region": {"bbox": [10, 10, 40, 30]}, "width": 30, "height": 20, "mime_type": "image/png"}]},
        "outcome": "technical_error", "result": None,
        "error": {"code": "VISION_NOT_SUPPORTED", "message": "未调用模型", "stage": "preflight", "retryable": False, "cause": None},
        "executor_kind": "service", "executor_name": "fixture", "requested_by": None, "agent_run_id": None,
        "provenance": {"provider_name": None, "model": None, "model_version": None, "prompt_version": None},
        "started_at": now, "completed_at": now,
    }]
    extracted.image_assessment = assessment
    session.commit()
    before = deepcopy(extracted.image_assessment["runs"])
    service.remove_staged(extracted.id, staged.id, actor_id=teacher.id)
    assert extracted.assets == [] and path.is_file()
    assert extracted.image_assessment["runs"] == before
    with pytest.raises(FileStorageError) as error:
        files.delete_unreferenced_bytes(locator)
    assert error.value.code == "FILE_IN_USE"
