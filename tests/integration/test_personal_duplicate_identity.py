"""Personal staging uses real Test PG and production material/source boundaries."""
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from backend.app.services.learning_material_service import MaterialError
from tests.integration import test_learning_material_versions as material_tests
from tests.integration.test_learning_material_versions import (
    subject,
    upload,
)

space = material_tests.space


def service(space):
    module = import_module("backend.app.services.paper_import_service")
    cls = getattr(module, "PersonalPaperImportService", None)
    assert cls is not None, "T035_IMPLEMENTATION_REQUIRED: personal paper service"
    return cls(space.subjects)


def source(space):
    sid = subject(space)
    doc = upload(space, sid, "paper.txt", b"1. Choose A or B. Answer: A", purpose="exam_paper").json()
    return sid, doc


def payload(doc, **changes):
    value = {
        "question_type": "single_choice",
        "stem_blocks": [{"type": "text", "text": "1. Choose A or B."}],
        "options": [{"id": key, "blocks": [{"type": "text", "text": key}]} for key in ("A", "B")],
        "source_locations": [{"document_id": doc["id"], "anchor": doc["blocks"][0]["anchor"]}],
    }
    value.update(changes)
    return value


def test_personal_import_has_no_virtual_course_or_user_and_is_idempotent(space):
    sid, doc = source(space)
    entry = service(space)
    first = entry.create(UUID(sid), UUID(doc["id"]))
    again = entry.create(UUID(sid), UUID(doc["id"]))
    assert first.id == again.id and first.subject_id == UUID(sid)
    with space.engine.connect() as connection:
        row = connection.execute(text(
            "SELECT course_id,uploaded_by,subject_id FROM paper_imports WHERE id=:id"
        ), {"id": first.id}).one()
    assert row[:2] == (None, None) and str(row[2]) == sid


def test_study_material_and_cross_subject_import_are_rejected(space):
    sid = subject(space)
    doc = upload(space, sid, "study.txt", b"Only teaching material").json()
    entry = service(space)
    with pytest.raises(MaterialError, match="PAPER_PURPOSE_REQUIRED"):
        entry.create(UUID(sid), UUID(doc["id"]))
    with pytest.raises(MaterialError, match="MATERIAL_NOT_FOUND"):
        entry.create(UUID(subject(space, "Other")), UUID(doc["id"]))


def test_missing_answer_remains_missing_and_cannot_become_eligible(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    entry.store_candidates(paper.id, [payload(doc)])
    draft = entry.get(paper.id).questions[0]
    assert draft.reference_answer is None and draft.answer_origin is None
    assert draft.question_id is None
    assert draft.question_type == "single_choice"


def test_model_asset_ids_must_belong_to_the_actual_import_source(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    data = payload(doc, stem_blocks=[{"type": "image", "asset_id": str(uuid4())}])
    with pytest.raises(MaterialError, match="PAPER_ASSET_SCOPE_INVALID"):
        entry.store_candidates(paper.id, [data])
    assert entry.get(paper.id).questions == []


def test_claimed_original_answer_excerpt_is_checked_against_real_source(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    loc = payload(doc)["source_locations"][0]
    with pytest.raises(MaterialError, match="PAPER_SOURCE_EVIDENCE_INVALID"):
        entry.store_candidates(paper.id, [payload(
            doc, reference_answer="B", answer_origin="paper",
            evidence={"reference_answer": {"source": loc, "text": "Answer: B"}},
        )])
    assert entry.get(paper.id).questions == []


def test_invalid_second_candidate_rolls_back_the_whole_staging_batch(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    invalid = payload(doc, stem_blocks=[{"type": "image", "asset_id": str(uuid4())}])
    with pytest.raises(MaterialError, match="PAPER_ASSET_SCOPE_INVALID"):
        entry.store_candidates(paper.id, [payload(doc), invalid])
    assert entry.get(paper.id).questions == []


def test_legacy_interruption_recovery_does_not_consume_personal_imports(space):
    from backend.app.core.config import get_settings
    from backend.app.services.paper_import_service import PaperImportRunner

    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    recovered = PaperImportRunner(space.subjects.sessions, settings=get_settings()).recover_interrupted()
    assert recovered == 0
    assert entry.get(paper.id).status == "Uploaded"


def test_guarded_extraction_requires_confirmation_and_sends_only_once(space, tmp_path):
    import asyncio
    import json

    import httpx

    from backend.app.services.learning_operations import (
        LearningOperations,
        OperationError,
    )
    from tests.support.runtime_settings_fakes import settings_harness

    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    calls = []
    harness = settings_harness(tmp_path / "fake-runtime")
    asyncio.run(harness.activate_model("test-model-a", with_credential=True))
    async def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "fake-personal-paper", "object": "chat.completion", "created": 0,
            "model": "test-model-a",
            "choices": [{"index": 0, "message": {"role": "assistant", "content":
                json.dumps({"questions": [payload(doc)]})}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        })
    operations = LearningOperations(
        sessions=space.subjects.sessions, config=harness.service,
        transport=httpx.MockTransport(handler),
    )
    try:
        operation = entry.prepare_extraction(paper.id, operations, uuid4().hex)
        with pytest.raises(OperationError, match="CONFIRMATION_REQUIRED"):
            asyncio.run(entry.execute_extraction(paper.id, operations, operation.id))
        assert calls == []
        operations.confirm(operation.id, preview_digest=operation.preview_digest)
        first = asyncio.run(entry.execute_extraction(paper.id, operations, operation.id))
        again = asyncio.run(entry.execute_extraction(paper.id, operations, operation.id))
        assert len(calls) == 1 and len(first.questions) == 1
        assert first.questions[0].id == again.questions[0].id
        assert operations.view(operation.id).actual_requests == 1
        assert operations.snapshots.active_lease_count == 0
    finally:
        asyncio.run(operations.aclose())
        harness.close()

def test_personal_confirmation_uses_one_atomic_idempotent_selection(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    entry.store_candidates(paper.id, [payload(doc), payload(doc)])
    drafts = entry.get(paper.id).questions
    from backend.app.ai.paper_extraction.schemas import PersonalPaperConfirm
    from backend.app.services.question_correction_service import (
        PersonalQuestionCorrection,
    )
    correction = PersonalQuestionCorrection(space.subjects)
    selection = PersonalPaperConfirm(
        question_ids=[q.id for q in drafts],
        expected_revisions={q.id: q.revision for q in drafts},
    )
    first = correction.confirm(paper.id, selection)
    again = correction.confirm(paper.id, selection)
    assert [q.question_id for q in first.questions] == [q.question_id for q in again.questions]
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM questions WHERE subject_id=:s"),
                                 {"s": sid}) == 2
        assert connection.scalar(text("SELECT count(*) FROM question_origins WHERE subject_id=:s"),
                                 {"s": sid}) == 2
        assert set(connection.execute(text(
            "SELECT admission_state FROM question_revisions WHERE subject_id=:s"
        ), {"s": sid}).scalars()) == {"incomplete"}


def test_confirmation_revision_conflict_rolls_back_every_selected_question(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    entry.store_candidates(paper.id, [payload(doc), payload(doc)])
    drafts = entry.get(paper.id).questions
    from backend.app.ai.paper_extraction.schemas import PersonalPaperConfirm
    from backend.app.services.question_correction_service import (
        PersonalQuestionCorrection,
    )
    selection = PersonalPaperConfirm(
        question_ids=[q.id for q in drafts],
        expected_revisions={drafts[0].id: 1, drafts[1].id: 2},
    )
    with pytest.raises(MaterialError, match="REVISION_CHANGED"):
        PersonalQuestionCorrection(space.subjects).confirm(paper.id, selection)
    assert all(q.question_id is None for q in entry.get(paper.id).questions)
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM questions WHERE subject_id=:s"),
                                 {"s": sid}) == 0


def test_duplicate_detection_requires_user_same_or_different_decision(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    entry.store_candidates(paper.id, [payload(doc), payload(doc)])
    from backend.app.ai.paper_extraction.schemas import PersonalPaperConfirm
    from backend.app.services.question_correction_service import (
        PersonalQuestionCorrection,
    )
    from backend.app.services.question_identity import QuestionIdentity
    drafts = entry.get(paper.id).questions
    PersonalQuestionCorrection(space.subjects).confirm(paper.id, PersonalPaperConfirm(
        question_ids=[q.id for q in drafts], expected_revisions={q.id: 1 for q in drafts},
    ))
    ids = QuestionIdentity(space.subjects)
    pairs = ids.candidates(UUID(sid))
    assert len(pairs) == 1
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM questions WHERE canonical_id IS NOT NULL")) == 0
    pair = pairs[0]
    ids.resolve(pair.id, decision="different", expected_revisions=pair.revisions)
    assert ids.candidates(UUID(sid)) == []


def test_personal_http_edit_preserves_missing_answer_and_sets_user_answer_provenance(space):
    sid, doc = source(space)
    created = space.client.post(f"/api/learning/subjects/{sid}/imports", json={"document_id": doc["id"]})
    assert created.status_code == 201, created.text
    identity = created.json()["id"]
    entry = service(space)
    entry.store_candidates(UUID(identity), [payload(doc)])
    draft = entry.get(UUID(identity)).questions[0]
    edit = {**payload(doc), "expected_revision": draft.revision, "reference_answer": "B",
            "uncertainties": ["Check original answer conflict"]}
    changed = space.client.patch(
        f"/api/learning/imports/{identity}/questions/{draft.id}", json=edit,
    )
    assert changed.status_code == 200, changed.text
    updated = changed.json()["questions"][0]
    assert updated["answer_origin"] == "user_correction" and updated["reference_answer"] == "B"
    assert updated["revision"] == 2
    stale = space.client.patch(f"/api/learning/imports/{identity}/questions/{draft.id}", json=edit)
    assert stale.status_code == 409 and stale.json()["code"] == "REVISION_CHANGED"
    response = space.client.post(f"/api/learning/imports/{identity}/confirm", json={
        "question_ids": [str(draft.id)], "expected_revisions": {str(draft.id): 2},
    })
    assert response.status_code == 200, response.text
    qid = response.json()["questions"][0]["question_id"]
    with space.engine.connect() as connection:
        row = connection.execute(text(
            "SELECT reference_answer,admission_state,grounding_refs FROM question_revisions WHERE question_id=:id"
        ), {"id": qid}).one()
    assert row[0:2] == ("B", "needs_validation")
    assert row[2]["uncertainties"] == ["Check original answer conflict"]


def test_personal_import_http_rejects_unknown_fields_and_unsupported_types(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    entry.store_candidates(paper.id, [payload(doc)])
    draft = entry.get(paper.id).questions[0]
    for invalid in (
        {"owner_id": str(uuid4())},
        {"question_type": "true_false"},
        {"expected_revision": False},
        {"score": 0},
    ):
        result = space.client.patch(
            f"/api/learning/imports/{paper.id}/questions/{draft.id}",
            json={**payload(doc), "expected_revision": 1, **invalid},
        )
        assert result.status_code == 422, result.text
    assert entry.get(paper.id).questions[0].revision == 1


def confirmed_pair(space):
    from backend.app.ai.paper_extraction.schemas import PersonalPaperConfirm
    from backend.app.services.question_correction_service import (
        PersonalQuestionCorrection,
    )
    from tests.integration.test_learning_material_versions import upload

    sid, first_doc = source(space)
    second_doc = upload(space, sid, "paper-v2.txt", b"1. Choose A or B. Answer: A\nNew edition",
                        purpose="exam_paper").json()
    ids = []
    for doc in (first_doc, second_doc):
        entry = service(space)
        paper = entry.create(UUID(sid), UUID(doc["id"]))
        entry.store_candidates(paper.id, [payload(doc)])
        draft = entry.get(paper.id).questions[0]
        result = PersonalQuestionCorrection(space.subjects).confirm(paper.id, PersonalPaperConfirm(
            question_ids=[draft.id], expected_revisions={draft.id: 1},
        ))
        ids.append(result.questions[0].question_id)
    return UUID(sid), ids


def add_history(space, subject_id, question_id, *, choice="A"):
    from backend.app.models.practice import (
        GradeAttempt,
        PracticeAnswer,
        PracticeItem,
        PracticeSession,
    )
    from backend.app.models.question import Question
    from backend.app.models.question_revision import QuestionRevision

    with space.subjects.sessions.begin() as session:
        question = session.get(Question, question_id)
        revision = session.get(QuestionRevision, question.current_revision_id)
        practice = PracticeSession(id=uuid4(), subject_id=subject_id, state="preview", conditions={})
        session.add(practice)
        session.flush()
        item = PracticeItem(
            id=uuid4(), session_id=practice.id, subject_id=subject_id, position=1,
            question_id=question.id, question_revision_id=revision.id,
            fixed_max_score=10, scoring_input={"test": "synthetic fixed basis"}, knowledge_point_ids=[],
        )
        session.add(item)
        session.flush()
        answer = PracticeAnswer(
            id=uuid4(), practice_item_id=item.id, answer_state="answered",
            submission_state="submitted", choice_ids=[choice],
        )
        session.add(answer)
        session.flush()
        practice.state = "completed"
        for number, score in ((1,0),(2,10)):
            session.add(GradeAttempt(
                id=uuid4(), answer_id=answer.id, round=number,
                validation_state="valid", execution_source="local_objective",
                score=score, max_score=10, reason="Synthetic stored history",
                scoring_input=item.scoring_input,
            ))
        return question.id, revision.id, item.id, answer.id


def test_concurrent_same_confirmation_preserves_both_sources_answers_and_rounds(space):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from backend.app.services.question_identity import QuestionIdentity, canonical_id

    sid, ids = confirmed_pair(space)
    before = [add_history(space, sid, identity) for identity in ids]
    entry = QuestionIdentity(space.subjects)
    pair = entry.candidates(sid)[0]
    gate = Barrier(2)
    def merge():
        gate.wait(timeout=10)
        return entry.resolve(pair.id, decision="same", expected_revisions=pair.revisions)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: merge(), range(2)))
    assert [r.decision for r in results] == ["same","same"]
    with space.subjects.sessions() as session:
        assert len({canonical_id(session, identity, sid) for identity in ids}) == 1
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM question_origins WHERE subject_id=:s"),{"s":sid}) == 2
        assert connection.scalar(text("SELECT count(DISTINCT document_id) FROM question_origins WHERE subject_id=:s"),{"s":sid}) == 2
        assert connection.scalar(text("SELECT count(*) FROM practice_answers")) == 2
        assert connection.scalar(text("SELECT count(*) FROM grade_attempts")) == 4
        for qid,rid,item_id,answer_id in before:
            assert connection.execute(text(
                "SELECT question_id,question_revision_id FROM practice_items WHERE id=:i"
            ),{"i":item_id}).one() == (qid,rid)
            assert connection.scalar(text("SELECT count(*) FROM grade_attempts WHERE answer_id=:a"),
                                     {"a":answer_id}) == 2
    assert entry.candidates(sid) == []


def test_stale_duplicate_revision_cannot_merge_changed_options(space):
    from backend.app.schemas.personal_learning import PersonalPaperEdit
    from backend.app.services.personal_question_service import PersonalQuestionService
    from backend.app.services.question_identity import QuestionIdentity

    sid, _ids = confirmed_pair(space)
    entry = QuestionIdentity(space.subjects)
    pair = entry.candidates(sid)[0]
    bank = PersonalQuestionService(space.subjects)
    old = bank.get(pair.right_id)
    data = old.model_dump(mode="json", include={
        "question_type","stem_blocks","options","explanation_blocks","source_locations",
        "reference_answer","scoring_rubric","knowledge_point_ids",
    })
    data["options"][0]["blocks"] = [{"type":"text","text":"Changed important condition"}]
    bank.revise(old.id, PersonalPaperEdit(**data, expected_revision=old.revision))
    with pytest.raises(MaterialError, match="REVISION_CHANGED"):
        entry.resolve(pair.id, decision="same", expected_revisions=pair.revisions)
    pending = entry.candidates(sid)
    assert len(pending) == 1 and "options" in pending[0].differences
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM questions WHERE canonical_id IS NOT NULL")) == 0


def test_local_ocr_worker_retains_slot_after_soft_timeout_and_keeps_real_original(space):
    import asyncio
    from threading import Event
    from types import SimpleNamespace

    from backend.app.ai.ingestion.ocr.schemas import OCRResult
    from tests.contract.test_learning_materials import png_bytes
    from tests.integration.test_learning_material_versions import upload

    sid = subject(space)
    doc = upload(space, sid, "scan.png", png_bytes("Original page"), purpose="exam_paper").json()
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    started, release = Event(), Event()
    closed = []
    class Client:
        async def extract_text(self, path):
            started.set()
            await asyncio.to_thread(release.wait, 5)
            return OCRResult(text="1. Choose A or B.", confidence=None, regions=[])
    class Runtime:
        def acquire(self, kind):
            assert kind == "ocr"
            return SimpleNamespace(client=Client())
        async def aclose(self):
            closed.append(True)
    snapshot = SimpleNamespace(
        snapshot_id=uuid4(), resolved_task_bindings={"ocr":SimpleNamespace(adapter_id="rapidocr",model_id="synthetic-local")}
    )
    operations = SimpleNamespace(
        snapshots=SimpleNamespace(create=lambda **_:snapshot,context=lambda _:Runtime()),
        limits=SimpleNamespace(local_wait_seconds=0.05),
    )
    async def run():
        try:
            first = await entry.process_local_ocr(paper.id, operations.snapshots, limits=operations.limits)
            assert started.is_set() and first.status == "Parsing" and closed == []
            with pytest.raises(MaterialError,match="PAPER_OCR_BUSY"):
                await entry.process_local_ocr(paper.id, operations.snapshots, limits=operations.limits)
        finally:
            release.set()
        for _ in range(200):
            if closed and not entry._ocr_slot.locked():
                break
            await asyncio.sleep(0.01)
        assert closed == [True] and not entry._ocr_slot.locked()
    asyncio.run(run())
    result = space.materials.get(UUID(doc["id"]))
    assert result.requires_ocr is False
    assert any(b.text == "1. Choose A or B." for b in result.blocks)
    assert any(b.type == "image" and b.sha256 == doc["assets"][0]["sha256"] for b in result.blocks)
    assert "OCR_REQUIRES_REVIEW" in result.issues
    assert entry.get(paper.id).status == "Uploaded"


def test_local_ocr_refuses_remote_adapter_without_any_call_or_status_change(space):
    import asyncio
    from types import SimpleNamespace

    from tests.contract.test_learning_materials import png_bytes
    from tests.integration.test_learning_material_versions import upload

    sid = subject(space)
    doc = upload(space, sid, "scan.png", png_bytes("Original"), purpose="exam_paper").json()
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    snapshot = SimpleNamespace(resolved_task_bindings={
        "ocr":SimpleNamespace(adapter_id="remote",model_id="not-local")
    })
    operations = SimpleNamespace(snapshots=SimpleNamespace(create=lambda **_:snapshot))
    with pytest.raises(MaterialError,match="LOCAL_OCR_REQUIRED"):
        asyncio.run(entry.process_local_ocr(paper.id,operations.snapshots))
    assert entry.get(paper.id).status == "Uploaded" and not entry._ocr_slot.locked()


def test_original_block_cannot_claim_another_document_or_object_source(space):
    from tests.contract.test_learning_materials import sample_docx
    from tests.integration.test_learning_material_versions import upload
    sid=subject(space)
    doc=upload(space,sid,"source.docx",sample_docx(),purpose="exam_paper").json()
    entry=service(space)
    paper=entry.create(UUID(sid),UUID(doc["id"]))
    asset=paper.assets[0]
    data=payload(doc,stem_blocks=[{
        "type":"original","asset_id":str(asset.id),"reason":"Needs original review",
        "source":{"document_id":str(uuid4()),"anchor":"fabricated"},
    }],source_locations=[asset.source.model_dump(mode="json")])
    with pytest.raises(MaterialError,match="PAPER_ASSET_SOURCE_INVALID"):
        entry.store_candidates(paper.id,[data])
    assert entry.get(paper.id).questions == []


def test_interrupted_local_ocr_is_failed_truthfully_without_automatic_resume(space):
    sid, doc=source(space)
    entry=service(space)
    paper=entry.create(UUID(sid),UUID(doc["id"]))
    with space.engine.begin() as connection:
        connection.execute(text("UPDATE paper_imports SET status='Parsing' WHERE id=:id"),{"id":paper.id})
    result=entry.get(paper.id)
    assert result.status=="Failed" and result.error_code=="PAPER_OCR_INTERRUPTED"
    assert result.questions==[]


def test_overlapping_concurrent_merges_have_one_root_and_no_cycles(space):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from backend.app.ai.paper_extraction.schemas import PersonalPaperConfirm
    from backend.app.services.question_correction_service import (
        PersonalQuestionCorrection,
    )
    from backend.app.services.question_identity import QuestionIdentity, canonical_id
    from tests.integration.test_learning_material_versions import upload

    sid,ids=confirmed_pair(space)
    doc=upload(space,str(sid),"third.txt",b"1. Choose A or B. Answer: A\nThird source",purpose="exam_paper").json()
    entry=service(space)
    paper=entry.create(sid,UUID(doc["id"]))
    entry.store_candidates(paper.id,[payload(doc)])
    draft=entry.get(paper.id).questions[0]
    created=PersonalQuestionCorrection(space.subjects).confirm(paper.id,PersonalPaperConfirm(
        question_ids=[draft.id],expected_revisions={draft.id:1},
    ))
    ids.append(created.questions[0].question_id)
    identity=QuestionIdentity(space.subjects)
    pairs=identity.candidates(sid)
    assert len(pairs)==3
    gate=Barrier(2)
    def merge(pair):
        gate.wait(timeout=10)
        return identity.resolve(pair.id,decision="same",expected_revisions=pair.revisions)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(merge,pairs[:2]))
    with space.subjects.sessions() as session:
        assert len({canonical_id(session,qid,sid) for qid in ids})==1
    assert identity.candidates(sid)==[]


def test_upload_after_personal_image_question_confirmation_keeps_subject_owner(space):
    from backend.app.ai.paper_extraction.schemas import PersonalPaperConfirm
    from backend.app.services.question_correction_service import (
        PersonalQuestionCorrection,
    )
    from tests.contract.test_learning_materials import png_bytes, sample_docx
    from tests.integration.test_learning_material_versions import upload

    sid=subject(space)
    doc=upload(space,sid,"figures.docx",sample_docx(),purpose="exam_paper").json()
    entry=service(space);paper=entry.create(UUID(sid),UUID(doc["id"]))
    asset=paper.assets[0]
    entry.store_candidates(paper.id,[payload(doc,
        stem_blocks=[{"type":"image","asset_id":str(asset.id)}],
        source_locations=[asset.source.model_dump(mode="json")],
    )])
    draft=entry.get(paper.id).questions[0]
    PersonalQuestionCorrection(space.subjects).confirm(paper.id,PersonalPaperConfirm(
        question_ids=[draft.id],expected_revisions={draft.id:1},
    ))
    result=upload(space,sid,"next.png",png_bytes("Next source"),purpose="exam_paper")
    assert result.status_code==201,result.text
    from backend.app.services.storage_migration_service import storage_references
    with space.subjects.sessions() as session:
        references=storage_references(session)
        image_refs=[r for r in references if r.file_id.startswith("a_")]
        assert len(image_refs)==1
        assert image_refs[0].owner["subject_id"]==sid and "course_id" not in image_refs[0].owner


def test_difficulty_can_be_cleared_without_changing_answer_or_source(space):
    from backend.app.schemas.personal_learning import PersonalPaperEdit
    from backend.app.services.personal_question_service import PersonalQuestionService
    _sid, ids = confirmed_pair(space)
    bank = PersonalQuestionService(space.subjects)
    first = bank.get(ids[0])
    fields = {"question_type", "stem_blocks", "options", "explanation_blocks",
              "source_locations", "reference_answer", "scoring_rubric", "knowledge_point_ids"}
    for difficulty in ("基础", None):
        first = bank.revise(first.id, PersonalPaperEdit(
            **first.model_dump(include=fields), expected_revision=first.revision, difficulty=difficulty,
        ))
        assert first.difficulty == difficulty
        assert first.source_kind == "uploaded"
