"""T154 visibility: real JWT, source bytes, existing exam/owner grants. TCR §11."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

from backend.app.domain.enums import ExamStatus, QuestionStatus, SubmissionStatus
from backend.app.models import (
    Answer,
    Exam,
    ExamParticipant,
    ExtractedQuestion,
    Question,
    QuestionAsset,
    Submission,
)
from backend.app.schemas.question_assets import AssetLinkRequest
from backend.app.services.question_service import QuestionService
from tests.contract.test_file_storage_api import files_api as _files_api
from tests.unit.services.test_question_asset_service import png, staged_case

files_api = _files_api


def question_and_asset(case):
    client, session, _files, doc, users, headers = case
    created = QuestionService(session).create_question(
        doc.course_id, "SHORT_ANSWER", "看图回答", reference_answer="原图依据",
        scoring_rubric="按依据给分", score=2, created_by=users[0].id,
    )
    question = session.get(Question, UUID(created.id))
    response = client.post(
        f"/api/questions/{question.id}/assets/upload", headers=headers[0],
        files={"file": ("figure.png", png(), "image/png")},
    )
    assert response.status_code == 201, response.text
    return question, response.json()


def visibility_url(question, asset):
    return f"/api/questions/{question.id}/assets/{asset['id']}/visibility"


def opened_exam(case, question, *, assigned=True):
    _client, session, _files, doc, users, _headers = case
    question.status = QuestionStatus.APPROVED
    exam = Exam(
        course_id=doc.course_id, created_by=users[0].id, title="题图考试",
        status=ExamStatus.PUBLISHED, questions=[question],
    )
    session.add(exam)
    session.flush()
    if assigned:
        session.add(ExamParticipant(exam_id=exam.id, student_id=users[2].id))
    session.commit()
    return exam


def test_default_false_teacher_switch_and_strict_bool(files_api):
    client, session, _files, _doc, _users, headers = files_api
    question, asset = question_and_asset(files_api)
    assert asset["student_visible"] is False
    url = visibility_url(question, asset)
    assert client.patch(url, headers=headers[0], json={"student_visible": 1}).status_code == 422
    assert client.patch(url, headers=headers[0], json={"student_visible": None}).status_code == 422
    assert client.patch(url, headers=headers[2], json={"student_visible": True}).status_code == 403
    assert client.patch(url, headers=headers[1], json={"student_visible": True}).status_code == 403
    session.refresh(question)
    original_context = deepcopy(question.image_assessment)
    for value in (True, False):
        response = client.patch(url, headers=headers[0], json={"student_visible": value})
        assert response.status_code == 200, response.text
        assert response.json()["student_visible"] is value
    session.expire_all()
    assert session.get(QuestionAsset, UUID(asset["id"])).student_visible is False
    assert session.get(Question, question.id).image_assessment == original_context
    listing = client.get(f"/api/questions/{question.id}/assets", headers=headers[0])
    assert listing.status_code == 200 and len(listing.json()) == 1
    assert client.get("/api/files/" + asset["file_id"], headers=headers[0]).content == png()
    assert client.get("/api/files/" + asset["file_id"], headers=headers[2]).status_code == 403


def test_only_enabled_assets_are_returned_and_download_requires_exam_grant(files_api):
    client, session, _files, _doc, _users, headers = files_api
    question, shown = question_and_asset(files_api)
    hidden = client.post(
        f"/api/questions/{question.id}/assets/upload", headers=headers[0],
        files={"file": ("hidden.png", png(), "image/png")},
    ).json()
    assert client.patch(visibility_url(question, shown), headers=headers[0], json={"student_visible": True}).status_code == 200
    # A true flag by itself does not make an unassigned question public.
    assert client.get("/api/files/" + shown["file_id"], headers=headers[2]).status_code == 403
    exam = opened_exam(files_api, question)
    detail = client.get(f"/api/submissions/exams/{exam.id}", headers=headers[2])
    assert detail.status_code == 200, detail.text
    assert [asset["id"] for asset in detail.json()["questions"][0]["assets"]] == [shown["id"]]
    assets = client.get(f"/api/questions/{question.id}/assets", headers=headers[2])
    assert assets.status_code == 200
    assert [asset["id"] for asset in assets.json()] == [shown["id"]]
    assert all(asset["student_visible"] is True for asset in assets.json())
    response = client.get("/api/files/" + shown["file_id"], headers=headers[2])
    assert response.status_code == 200 and response.content == png()
    assert client.get("/api/files/" + hidden["file_id"], headers=headers[2]).status_code == 403
    for path in (f"/api/questions/{question.id}/assets", "/api/files/" + shown["file_id"]):
        assert client.get(path, headers=headers[3]).status_code == 403
        assert client.get(path).status_code == 401
    assert len(client.get(f"/api/questions/{question.id}/assets", headers=headers[0]).json()) == 2
    assert client.patch(visibility_url(question, shown), headers=headers[0], json={"student_visible": False}).status_code == 409
    # Read-side policy must honor current persisted false, including migration/default records.
    session.expire_all()
    session.get(QuestionAsset, UUID(shown["id"])).student_visible = False
    session.commit()
    assert client.get(f"/api/questions/{question.id}/assets", headers=headers[2]).json() == []
    assert client.get("/api/files/" + shown["file_id"], headers=headers[2]).status_code == 403


def test_exam_window_and_own_submitted_answer_are_distinct_grants(files_api):
    client, session, _files, doc, users, headers = files_api
    question, asset = question_and_asset(files_api)
    assert client.patch(visibility_url(question, asset), headers=headers[0], json={"student_visible": True}).status_code == 200
    exam = opened_exam(files_api, question)
    path = "/api/files/" + asset["file_id"]
    exam.starts_at = datetime.now(UTC) + timedelta(hours=1)
    session.commit()
    assert client.get(path, headers=headers[2]).status_code == 403
    exam.starts_at = None
    exam.ends_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert client.get(path, headers=headers[2]).status_code == 403
    exam.status = ExamStatus.CLOSED
    submission = Submission(exam_id=exam.id, student_id=users[2].id, status=SubmissionStatus.SUBMITTED, submitted_at=datetime.now(UTC))
    submission.answers.append(Answer(question_id=question.id, content="本人答案", status="Submitted"))
    session.add(submission)
    session.commit()
    assert client.get(path, headers=headers[2]).status_code == 200
    assert client.get(path, headers=headers[3]).status_code == 403
    assert client.get(f"/api/questions/{question.id}/assets", headers=headers[2]).status_code == 200
    other = QuestionService(session).create_question(doc.course_id, "SHORT_ANSWER", "其他题", created_by=users[0].id)
    assert client.get(f"/api/questions/{other.id}/assets", headers=headers[2]).status_code == 403


def test_full_pages_and_same_byte_aliases_stay_private_even_if_true(files_api):
    client, session, files, doc, users, headers = files_api
    service, page, _extracted = staged_case((session, doc, users[0], files.root))
    question, _uploaded = question_and_asset(files_api)
    payload = {"file_id": "p_" + page.id.hex, "source_page_id": str(page.id), "asset_type": "figure"}
    endpoint = f"/api/questions/{question.id}/assets"
    forbidden = client.post(endpoint, headers=headers[0], json=payload | {"student_visible": True})
    assert forbidden.status_code == 422, forbidden.text
    full = client.post(endpoint, headers=headers[0], json=payload)
    assert full.status_code == 201, full.text
    full_asset = full.json()
    assert client.patch(visibility_url(question, full_asset), headers=headers[0], json={"student_visible": True}).status_code == 422
    crop = client.post(endpoint, headers=headers[0], json=payload | {
        "region": {"bbox": [10, 10, 40, 30]}, "student_visible": True,
    })
    assert crop.status_code == 201, crop.text
    # A manually uploaded alias of a known complete page must not bypass its source policy.
    assert client.patch(visibility_url(question, _uploaded), headers=headers[0], json={"student_visible": True}).status_code == 422
    session.expire_all()
    for identity in (full_asset["id"], _uploaded["id"]):
        session.get(QuestionAsset, UUID(identity)).student_visible = True
    session.commit()
    exam = opened_exam(files_api, question)
    assets = client.get(endpoint, headers=headers[2])
    assert assets.status_code == 200
    assert [asset["id"] for asset in assets.json()] == [crop.json()["id"]]
    assert len(client.get(endpoint, headers=headers[0]).json()) == 3
    for file_id in (full_asset["file_id"], _uploaded["file_id"], "p_" + page.id.hex, "d_" + page.paper_import.document_id.hex):
        assert client.get("/api/files/" + file_id, headers=headers[2]).status_code == 403
    assert client.get("/api/files/" + crop.json()["file_id"], headers=headers[2]).status_code == 200
    detail = client.get(f"/api/submissions/exams/{exam.id}", headers=headers[2]).json()
    assert [asset["id"] for asset in detail["questions"][0]["assets"]] == [crop.json()["id"]]
    assert service.list_question(question.id, actor_id=users[0].id)


def test_staged_switch_survives_formal_mapping_without_rewriting_origin(files_api):
    client, session, files, doc, users, headers = files_api
    service, page, extracted = staged_case((session, doc, users[0], files.root))
    staged = service.create_staged(extracted.id, AssetLinkRequest(
        file_id="p_" + page.id.hex, source_page_id=page.id, asset_type="diagram",
        region={"bbox": [10, 10, 40, 30]}, student_visible=True,
    ), actor_id=users[0].id)
    assert staged.student_visible is True
    assert client.get("/api/files/" + staged.file_id, headers=headers[2]).status_code == 403
    original = deepcopy(extracted.assets)
    question = Question(course_id=doc.course_id, type="SHORT_ANSWER", content="原题", reference_answer="依据", scoring_rubric="按依据给分", knowledge_points=[], score=2, created_by=users[0].id, source_type="paper_imported")
    extracted.question, extracted.status = question, "Corrected"
    asset = QuestionAsset(id=staged.id, question=question, asset_type="diagram", source_page=page, region={"bbox": [10, 10, 40, 30]}, width=30, height=20, order_index=1, student_visible=staged.student_visible)
    session.add(asset)
    session.commit()
    session.expire_all()
    assert session.get(QuestionAsset, staged.id).student_visible is True
    assert session.get(ExtractedQuestion, extracted.id).assets == original
    opened_exam(files_api, question)
    response = client.get("/api/files/" + staged.file_id, headers=headers[2])
    assert response.status_code == 200 and response.content != png()
