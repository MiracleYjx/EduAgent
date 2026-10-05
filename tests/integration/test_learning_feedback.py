"""T181 real PostgreSQL JSONB/source grants, no cloud calls (TCR §36)."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.api.results import ResultsQueryService
from backend.app.core.config import get_settings
from backend.app.domain.enums import (
    DocumentPurpose,
    DocumentStatus,
    QuestionStatus,
    QuestionType,
    UserRole,
)
from backend.app.models import (
    Answer,
    Course,
    DiagnosisReport,
    Document,
    DocumentChunk,
    ExamResult,
    KnowledgeBase,
    Question,
    QuestionValidationResult,
    WorkflowRun,
)
from backend.app.schemas.grading import DiagnosisReportDTO, DiagnosisStatus
from backend.app.services.grading.diagnosis_report_store import DiagnosisReportStore
from backend.app.services.question_service import QuestionService
from tests.contract import test_results_api_contract as result_contracts
from tests.contract.test_results_analysis_api import analysis_scenario, save_result
from tests.contract.test_results_api_contract import headers
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.question_validation_fixtures import persist_current_semantic_pass
from tests.unit.services.test_student_fixed_exam import add_image
from tests.unit.services.test_submission_service import add_user

client_factory = result_contracts.client_factory


@pytest.fixture
def session():
    with isolated_postgres_engine() as engine, Session(engine) as session:
        yield session


@pytest.fixture
def feedback_scenario(session, tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    s = analysis_scenario.__wrapped__(session)
    s["root"] = tmp_path
    s["service"] = ResultsQueryService(
        session=session,
        repository=s["repository"],
        diagnosis_store=DiagnosisReportStore(
            session_factory=lambda: Session(session.get_bind())
        ),
        learning_root=tmp_path,
    )
    s["final"] = save_result(session, s, 0, ["2", "2"])
    s["path"] = f"/api/results/me/submissions/{s['submissions'][0].id}/learning"
    s["headers"] = headers(s["students"][0], UserRole.STUDENT)
    yield s
    get_settings.cache_clear()


def material(
    session,
    s,
    *,
    labels=None,
    confirmed=True,
    status=DocumentStatus.READY,
    course=None,
    purpose=DocumentPurpose.KNOWLEDGE_BASE,
):
    course = course or s["course"]
    kb = KnowledgeBase(course_id=course.id, name=uuid4().hex)
    session.add(kb)
    session.flush()
    doc = Document(
        course_id=course.id,
        knowledge_base_id=kb.id if purpose == DocumentPurpose.KNOWLEDGE_BASE else None,
        uploaded_by=s["teacher"].id,
        original_filename="真实学习片段.txt",
        file_format="txt",
        status=status,
        purpose=purpose,
    )
    session.add(doc)
    session.flush()
    metadata = {"knowledge_points": labels or ["一次函数", "表达与计算"]}
    if confirmed:
        metadata["scope_confirmation"] = {
            "knowledge_points": {
                "teacher_id": str(s["teacher"].id),
                "confirmed_at": datetime.now(UTC).isoformat(),
            }
        }
    chunk = DocumentChunk(
        document_id=doc.id,
        course_id=course.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="一次函数表达与计算的真实受控教学内容。",
        chunk_metadata=metadata,
    )
    session.add(chunk)
    session.commit()
    return chunk


def practice(session, s, *, image=False):
    question = Question(
        course_id=s["course"].id,
        created_by=s["teacher"].id,
        type=QuestionType.SINGLE_CHOICE,
        content="真实练习：选择一次函数表达式。",
        options={"D": "y=x+1", "A": "y=2x", "B": "y=x*x"},
        reference_answer="A",
        scoring_rubric="正确选项给满分。",
        score=Decimal(2),
        knowledge_points=["一次函数", "表达与计算"],
        status=QuestionStatus.PENDING_REVIEW,
    )
    session.add(question)
    session.commit()
    asset, path, data = None, None, None
    if image:
        info = SimpleNamespace(
            objective_question_id=question.id, teacher_id=s["teacher"].id
        )
        asset, path, data = add_image((session, info, s["root"]))
    persist_current_semantic_pass(session, question.id, s["teacher"].id, root=s["root"])
    QuestionService(session).update_question_status(
        question.id, QuestionStatus.APPROVED, teacher_id=s["teacher"].id
    )
    return question, asset, path, data


def read(client, s):
    response = client.get(s["path"], headers=s["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def test_final_feedback_real_sources_answers_and_authorized_details(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    chunk = material(session, s)
    q, _, _, _ = practice(session, s)
    client = client_factory(s["service"])
    b = read(client, s)
    assert b["is_final"] and len(b["items"]) == 2
    assert (
        b["items"][1]["student_answer"] == "B" and b["items"][1]["lost_score"] == "1.00"
    )
    assert [g["knowledge_point"] for g in b["recommendations"]] == [
        "一次函数",
        "表达与计算",
    ]
    group = b["recommendations"][0]
    assert (group["awarded_score"], group["maximum_score"], group["lost_score"]) == (
        "4.00",
        "5.00",
        "1.00",
    )
    assert group["answer_ids"] == [b["items"][1]["answer_id"]]
    assert group["materials"][0]["chunk_id"] == str(chunk.id)
    assert group["practices"][0]["question_id"] == str(q.id)
    assert group["practices"][0]["sources"][0]["kind"] == "saved_question_source_chunk"
    m = client.get(group["materials"][0]["url"], headers=s["headers"])
    p = client.get(group["practices"][0]["url"], headers=s["headers"])
    assert m.status_code == 200 and m.json()["content"] == chunk.content
    assert p.status_code == 200 and list(p.json()["options"]) == ["D", "A", "B"]
    for key in [
        "reference_answer",
        "scoring_rubric",
        "storage_path",
        "source_page_id",
        "file_metadata",
        "image_data",
    ]:
        assert key not in p.json() and key not in str(b)
    assert (
        b["diagnosis"]["status"] == "Not Ready"
    )  # real report missing, no synthetic successful report


def test_missing_and_untrusted_resources_do_not_get_replacements(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    material(session, s, confirmed=False)
    material(session, s, labels=["一次函数拓展"])
    material(session, s, status=DocumentStatus.FAILED)
    material(session, s, purpose=DocumentPurpose.PAPER_SOURCE)
    q, _, _, _ = practice(session, s)
    q.status = QuestionStatus.NEEDS_REVISION
    session.commit()
    b = read(client_factory(s["service"]), s)
    assert all(
        g["materials"] == [] and g["material_not_ready_reason"]
        for g in b["recommendations"]
    )
    assert all(
        g["practices"] == [] and g["practice_not_ready_reason"]
        for g in b["recommendations"]
    )


def test_other_course_and_arbitrary_same_course_ids_are_denied(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    selected = material(session, s)
    foreign = Course(name="外课程", created_by=s["teacher"].id)
    session.add(foreign)
    session.commit()
    outside = material(session, s, course=foreign)
    unrelated = material(session, s, labels=["无关标签"])
    q, _, _, _ = practice(session, s)
    q.course_id = foreign.id
    session.commit()
    client = client_factory(s["service"])
    b = read(client, s)
    assert {m["chunk_id"] for g in b["recommendations"] for m in g["materials"]} == {
        str(selected.id)
    }
    assert all(g["practices"] == [] for g in b["recommendations"])
    for chunk in [outside, unrelated]:
        assert (
            client.get(
                f"{s['path']}/materials/{chunk.id}", headers=s["headers"]
            ).status_code
            == 403
        )
    assert (
        client.get(f"{s['path']}/practices/{q.id}", headers=s["headers"]).status_code
        == 403
    )


def test_regrading_revokes_old_links_and_keeps_report_stale(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    material(session, s)
    practice(session, s)
    store = s["service"]._diagnosis_store
    store.save(
        DiagnosisReportDTO(
            exam_result_id=f"exam-result:{s['submissions'][0].id}",
            submission_id=str(s["submissions"][0].id),
            student_id=str(s["students"][0].id),
            status=DiagnosisStatus.READY,
            generated_at=datetime.now(UTC),
            source_exam_result_updated_at=s["final"].aggregated_at,
        )
    )
    client = client_factory(s["service"])
    old = read(client, s)
    url = old["recommendations"][0]["materials"][0]["url"]
    save_result(session, s, 0, ["1", "1"], pending=True)
    current = read(client, s)
    assert (
        not current["is_final"]
        and current["recommendations"] == []
        and current["weak_knowledge_points"] == []
    )
    assert current["not_ready_reason"] and current["diagnosis"]["status"] == "Stale"
    assert client.get(url, headers=s["headers"]).status_code == 403
    assert session.scalar(select(func.count()).select_from(DiagnosisReport)) == 1


def test_get_is_read_only_and_preserves_current_final_report_link(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    material(session, s)
    practice(session, s)
    store = s["service"]._diagnosis_store
    saved = store.save(
        DiagnosisReportDTO(
            exam_result_id=f"exam-result:{s['submissions'][0].id}",
            submission_id=str(s["submissions"][0].id),
            student_id=str(s["students"][0].id),
            status=DiagnosisStatus.READY,
            generated_at=datetime.now(UTC),
            source_exam_result_updated_at=s["final"].aggregated_at,
        )
    )
    tables = [
        ExamResult,
        DiagnosisReport,
        WorkflowRun,
        QuestionValidationResult,
        Answer,
    ]
    before = [session.scalar(select(func.count()).select_from(t)) for t in tables]
    client = client_factory(s["service"])
    b = read(client, s)
    read(client, s)
    after = [session.scalar(select(func.count()).select_from(t)) for t in tables]
    assert after == before
    assert b["diagnosis"]["exam_result_id"] == saved.exam_result_id
    assert (
        b["source_exam_result_updated_at"]
        == b["diagnosis"]["source_exam_result_updated_at"]
    )


@pytest.mark.parametrize("role", [UserRole.STUDENT, UserRole.TEACHER, UserRole.ADMIN])
def test_role_and_own_submission_boundaries(
    session, feedback_scenario, client_factory, role
):
    s = feedback_scenario
    actor = add_user(session, role, username="other", email="other@example.com")
    client = client_factory(s["service"])
    assert client.get(s["path"], headers=headers(actor, role)).status_code == 403
    assert client.get(s["path"]).status_code == 401


def test_practice_original_image_is_authorized_and_exact(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    _q, asset, path, data = practice(session, s, image=True)
    client = client_factory(s["service"])
    b = read(client, s)
    recommended = b["recommendations"][0]["practices"][0]
    response = client.get(recommended["assets"][0]["url"], headers=s["headers"])
    assert (
        response.status_code == 200
        and response.content == data
        and response.headers["content-type"] == "image/png"
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert str(path) not in str(b)
    # New own-result grant does not open all files or the teacher management API.
    from backend.app.services.file_storage_service import (
        FileStorageError,
        FileStorageService,
    )

    with pytest.raises(FileStorageError) as denied:
        FileStorageService(session, root=s["root"]).download(
            asset.file_id, actor_id=s["students"][0].id
        )
    assert denied.value.code == "FILE_FORBIDDEN"
    asset.student_visible = False
    session.commit()
    now = read(client, s)
    assert all(g["practices"] == [] for g in now["recommendations"])
    assert (
        client.get(recommended["assets"][0]["url"], headers=s["headers"]).status_code
        == 403
    )


@pytest.mark.parametrize(
    "change,code,status",
    [("missing", "FILE_MISSING", 404), ("changed", "FILE_CONTENT_CHANGED", 409)],
)
def test_image_file_failure_stays_real_and_does_not_change_final_score(
    session, feedback_scenario, client_factory, change, code, status
):
    s = feedback_scenario
    _q, _asset, path, _ = practice(session, s, image=True)
    client = client_factory(s["service"])
    b = read(client, s)
    url = b["recommendations"][0]["practices"][0]["assets"][0]["url"]
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed bytes")
    response = client.get(url, headers=s["headers"])
    assert (
        response.status_code == status
        and response.json()["detail"]["error_code"] == code
    )
    assert s["repository"].get_exam_result(
        str(s["submissions"][0].id)
    ).final_total_score == Decimal(4)


def test_latest_unknown_approval_evidence_is_not_reused(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    q, _, _, _ = practice(session, s)
    q.validation_revision += 1
    session.commit()
    b = read(client_factory(s["service"]), s)
    assert all(
        g["practices"] == [] and g["practice_not_ready_reason"]
        for g in b["recommendations"]
    )


def test_full_score_and_unknown_labels_do_not_manufacture_weakness(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    save_result(session, s, 0, ["2", "3"])
    b = read(client_factory(s["service"]), s)
    assert b["recommendations"] == [] and b["weak_knowledge_points"] == []
    save_result(session, s, 0, ["2", "2"])
    s["exam"].exam_question_links[1].published_knowledge_points = None
    session.commit()
    b = read(client_factory(s["service"]), s)
    assert b["recommendations"] == [] and b["insufficient_evidence_answer_ids"]


def test_source_scan_alias_is_permanently_private_even_with_visibility_flag(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    q, asset, _path, _data = practice(session, s, image=True)
    source = Document(
        course_id=s["course"].id,
        knowledge_base_id=None,
        purpose=DocumentPurpose.PAPER_SOURCE,
        uploaded_by=s["teacher"].id,
        original_filename="source-scan.png",
        file_format="png",
        status=DocumentStatus.READY,
        file_metadata=asset.file_metadata,
    )
    session.add(source)
    session.commit()
    assert asset.student_visible is True
    client = client_factory(s["service"])
    b = read(client, s)
    assert all(group["practices"] == [] for group in b["recommendations"])
    url = f"{s['path']}/practices/{q.id}/assets/{asset.id}"
    assert client.get(url, headers=s["headers"]).status_code == 403


def test_current_incomplete_approved_row_is_not_a_practice_replacement(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    q, _, _, _ = practice(session, s)
    q.reference_answer = None
    session.commit()
    b = read(client_factory(s["service"]), s)
    assert all(
        group["practices"] == [] and group["practice_not_ready_reason"]
        for group in b["recommendations"]
    )


def test_own_original_result_image_is_exact_and_private_attachment_is_explicit(
    session, feedback_scenario, client_factory
):
    s = feedback_scenario
    info = SimpleNamespace(
        objective_question_id=s["questions"][0].id, teacher_id=s["teacher"].id
    )
    asset, _path, data = add_image((session, info, s["root"]))
    client = client_factory(s["service"])
    b = read(client, s)
    url = b["items"][0]["assets"][0]["url"]
    response = client.get(url, headers=s["headers"])
    assert (
        response.status_code == 200
        and response.content == data
        and response.headers["content-type"] == "image/png"
    )
    asset.student_visible = False
    session.commit()
    b = read(client, s)
    assert b["items"][0]["assets"] == [] and b["items"][0]["image_unavailable_reason"]
    assert client.get(url, headers=s["headers"]).status_code == 403
