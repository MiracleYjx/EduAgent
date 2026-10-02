"""T154 PG JSONB/identity projection and shared-file maintenance. TCR §10."""
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from backend.app.models import (
    Course,
    Document,
    ExtractedQuestion,
    PaperImport,
    Question,
    QuestionAsset,
    Role,
    User,
)
from backend.app.schemas.image_assessment import ImageAssessment
from backend.app.schemas.question_assets import AssetLinkRequest
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.question_asset_service import QuestionAssetService
from backend.app.services.question_service import QuestionService
from backend.app.services.storage_migration_service import storage_references
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.services.test_question_asset_service import png


def test_staging_to_formal_projects_same_identity_and_retains_origin(tmp_path):
    with isolated_postgres_engine() as engine, Session(engine, expire_on_commit=False) as session:
        teacher = User(username="e2_teacher", email="e2@example.test", password_hash="unused", roles=[Role(name="Teacher")])
        session.add(teacher)
        session.flush()
        course = Course(name="E2", created_by=teacher.id)
        session.add(course)
        session.flush()
        source = Document(id=uuid4(), course_id=course.id, purpose="paper_source", uploaded_by=teacher.id, original_filename="original.png", file_format="png", status="Ready")
        files = FileStorageService(session, root=tmp_path)
        stored = files.store_document(source, png(), actor_id=teacher.id)
        source.storage_path, source.file_metadata = stored.storage_path, stored.metadata.model_dump(mode="json")
        imported = PaperImport(course_id=course.id, uploaded_by=teacher.id, document=source, original_filename="original.png", page_count=1, status="Parsing")
        session.add(imported)
        session.commit()
        files.commit_receipt(stored, current_status="Ready")
        service = QuestionAssetService(session, root=tmp_path)
        page = service.create_source_page(imported.id, page_number=1, content=png(), actor_id=teacher.id)
        imported.status = "Pending Review"
        extracted = ExtractedQuestion(paper_import_id=imported.id, source_page_ids=[str(page.id)], extracted_by="TEXT", status="Pending Correction", assets=[])
        session.add(extracted)
        session.commit()
        staged = service.create_staged(extracted.id, AssetLinkRequest(file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="diagram", region={"bbox": [10, 10, 40, 30]}), actor_id=teacher.id)
        original = deepcopy(extracted.assets)
        question = Question(course_id=course.id, type="SHORT_ANSWER", content="原题", knowledge_points=[], score=2, created_by=teacher.id, source_type="paper_imported")
        extracted.question = question
        extracted.status = "Corrected"
        asset = QuestionAsset(id=staged.id, question=question, asset_type="diagram", source_page=page, region={"bbox": [10, 10, 40, 30]}, width=30, height=20, order_index=1)
        session.add(asset)
        session.commit()
        session.expire_all()
        fresh = session.get(QuestionAsset, staged.id)
        assert fresh._file_path is None and fresh._file_metadata is None
        assert fresh.file_path == original[0]["file_meta"]["storage_path"]
        assert files.get_view(staged.file_id, actor_id=teacher.id).resource_type == "question_asset"
        references = storage_references(session)
        assert [reference.file_id for reference in references].count(staged.file_id) == 1
        with pytest.raises(FileStorageError) as error:
            files.delete_unreferenced_bytes(fresh.file_path)
        assert error.value.code == "FILE_IN_USE"
        with pytest.raises(ValueError):
            fresh.file_path = "assets/another.png"
        assert session.get(ExtractedQuestion, extracted.id).assets == original
        actual = next(column for column in inspect(engine).get_columns("question_assets") if column["name"] == "region")
        assert str(actual["type"]) == "JSONB"


def test_g05_context_change_preserves_real_error_and_round_identity(owned_document):
    session, doc, teacher, _root = owned_document
    questions = QuestionService(session)
    created = questions.create_question(doc.course_id, "SHORT_ANSWER", "A", created_by=teacher.id)
    from uuid import UUID
    question = session.get(Question, UUID(created.id))
    now, identity, asset_id = datetime.now(UTC), uuid4(), uuid4()
    assessment = ImageAssessment.model_validate({
        "context_revision": 0, "manual_checks": [], "imported_review": None,
        "runs": [{
            "id": str(identity), "run_no": 1, "context_revision": 0, "task": "条件理解",
            "input_refs": {"text_fields": ["content"], "images": [{"asset_id": str(asset_id), "file_id": "a_" + asset_id.hex, "image_index": 1, "asset_type": "figure", "source_page_id": None, "region": None, "width": None, "height": None, "mime_type": None}]},
            "outcome": "technical_error", "result": None,
            "error": {"code": "VISION_PROVIDER_NOT_READY", "message": "未配置模型", "stage": "preflight", "retryable": False, "cause": None},
            "executor_kind": "service", "executor_name": "fixture", "requested_by": None, "agent_run_id": None,
            "provenance": {"provider_name": None, "model": None, "model_version": None, "prompt_version": None},
            "started_at": now.isoformat(), "completed_at": now.isoformat(),
        }],
    })
    question.image_assessment = assessment.model_dump(mode="json")
    session.commit()
    before = deepcopy(question.image_assessment["runs"])
    questions.update_question(created.id, content="B", teacher_id=teacher.id)
    questions.update_question(created.id, content="A", teacher_id=teacher.id)
    assert question.image_assessment["context_revision"] == 2
    assert question.image_assessment["runs"] == before
    assert question.image_assessment["runs"][0]["error"]["code"] == "VISION_PROVIDER_NOT_READY"
    assert question.image_assessment["manual_checks"] == []

from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)

owned_document = _owned_document

def test_real_backup_restores_original_pages_staged_and_formal_assets(backup_case):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from tests.integration.test_backup_restore import backup, restore
    service, ids, tmp_path, _token, name = backup_case
    with Session(service.engine, expire_on_commit=False) as session:
        files = FileStorageService(session, root=service.root)
        source = Document(id=uuid4(), course_id=ids["course"], purpose="paper_source", uploaded_by=ids["teacher"], original_filename="original.png", file_format="png", status="Ready")
        stored = files.store_document(source, png(), actor_id=ids["teacher"])
        source.storage_path, source.file_metadata = stored.storage_path, stored.metadata.model_dump(mode="json")
        imported = PaperImport(course_id=ids["course"], uploaded_by=ids["teacher"], document=source, original_filename="original.png", page_count=1, status="Parsing")
        session.add(imported)
        session.commit()
        files.commit_receipt(stored, current_status="Ready")
        assets = QuestionAssetService(session, root=service.root)
        page = assets.create_source_page(imported.id, page_number=1, content=png(), actor_id=ids["teacher"])
        imported.status = "Pending Review"
        pending, corrected = [ExtractedQuestion(paper_import_id=imported.id, source_page_ids=[str(page.id)], extracted_by="TEXT", status="Pending Correction", assets=[]) for _ in range(2)]
        session.add_all([pending, corrected])
        session.commit()
        staged = assets.create_staged(pending.id, AssetLinkRequest(file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="figure"), actor_id=ids["teacher"])
        crop = assets.create_staged(corrected.id, AssetLinkRequest(file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="diagram", region={"bbox": [10, 10, 40, 30]}), actor_id=ids["teacher"])
        origin = deepcopy(corrected.assets)
        original_assessment = deepcopy(corrected.image_assessment)
        question = Question(course_id=ids["course"], type="SHORT_ANSWER", content="原题", analysis=None, knowledge_points=[], score=2, created_by=ids["teacher"], source_type="paper_imported")
        corrected.question = question
        corrected.status = "Corrected"
        formal = QuestionAsset(id=crop.id, question=question, source_page=page, asset_type="diagram", region={"bbox": [10, 10, 40, 30]}, width=30, height=20, order_index=1)
        session.add(formal)
        session.commit()
        identifiers = source.id, page.id, pending.id, corrected.id, question.id
        crop_bytes = files.download(crop.file_id, actor_id=ids["teacher"])[0].read_bytes()
    destination, manifest = backup(backup_case)
    assert manifest.outcome == "complete", manifest.issues
    by_id = {entry.file_id: entry for entry in manifest.references}
    assert by_id["p_" + page.id.hex].resource_type == "source_page"
    assert by_id[staged.file_id].resource_type == "staged_asset"
    assert by_id[crop.file_id].resource_type == "question_asset"
    assert by_id[staged.file_id].relative_path == by_id["p_" + page.id.hex].relative_path
    assert by_id[crop.file_id].owner["extracted_question_id"] == str(identifiers[3])
    assert restore(backup_case, destination).outcome == "verified"
    restored = create_engine(service.engine.url.set(database=name), poolclass=NullPool)
    try:
        with Session(restored) as session:
            files = FileStorageService(session, root=tmp_path / "restored")
            assert files.download("d_" + identifiers[0].hex, actor_id=ids["teacher"])[0].read_bytes() == png()
            assert files.download("p_" + identifiers[1].hex, actor_id=ids["teacher"])[0].read_bytes() == png()
            assert files.download(staged.file_id, actor_id=ids["teacher"])[0].read_bytes() == png()
            assert files.download(crop.file_id, actor_id=ids["teacher"])[0].read_bytes() == crop_bytes
            formal = session.get(QuestionAsset, crop.id)
            assert formal._file_path is None
            assert session.get(ExtractedQuestion, identifiers[3]).assets == origin
            assert session.get(ExtractedQuestion, identifiers[3]).image_assessment == original_assessment
            for identity in ("d_" + identifiers[0].hex, "p_" + identifiers[1].hex):
                with pytest.raises(FileStorageError) as error:
                    files.download(identity, actor_id=ids["student"])
                assert error.value.http_status == 403
    finally:
        restored.dispose()

from tests.integration.test_backup_restore import backup_case as _backup_case

backup_case = _backup_case
