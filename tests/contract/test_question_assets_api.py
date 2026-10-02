"""T154 real JWT/HTTP teacher assets, no private source bytes leak. TCR §10."""
from uuid import UUID

from backend.app.models import Question
from backend.app.services.question_service import QuestionService
from tests.contract.test_file_storage_api import files_api as _files_api
from tests.unit.services.test_question_asset_service import png

files_api = _files_api


def test_teacher_upload_list_and_student_source_denial(files_api):
    client, session, _files, doc, users, headers = files_api
    question = QuestionService(session).create_question(doc.course_id, "SHORT_ANSWER", "原图问题", created_by=users[0].id)
    url = f"/api/questions/{question.id}/assets/upload"
    assert client.post(url, files={"file": ("figure.png", png(), "image/png")}).status_code == 401
    assert client.post(url, headers=headers[1], files={"file": ("figure.png", png(), "image/png")}).status_code == 403
    response = client.post(url, headers=headers[0], files={"file": ("figure.png", png(), "image/png")}, data={"asset_type": "diagram"})
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["width"] == 120 and asset["height"] == 80 and asset["order_index"] == 1
    assert "file_path" not in asset and "file_metadata" not in asset
    downloaded = client.get("/api/files/" + asset["file_id"], headers=headers[0])
    assert downloaded.status_code == 200 and downloaded.content == png()
    assert client.get("/api/files/" + asset["file_id"], headers=headers[2]).status_code == 403
    listing = client.get(f"/api/questions/{question.id}/assets", headers=headers[0])
    assert listing.status_code == 200 and len(listing.json()) == 1
    assert client.get(f"/api/questions/{question.id}/assets", headers=headers[2]).status_code == 403
    assert session.get(Question, UUID(question.id)).image_assessment is not None


def test_parse_omission_and_approved_clear_share_teacher_api(files_api):
    client, _session, _files, doc, _users, headers = files_api
    response = client.post("/api/questions", headers=headers[0], json={
        "course_id": str(doc.course_id), "type": "SHORT_ANSWER", "content": "问题",
        "analysis": "真实解析", "score": "2.00",
    })
    assert response.status_code == 201
    identity = response.json()["id"]
    assert response.json()["source_type"] == "manual"
    url = f"/api/questions/{identity}"
    assert client.patch(url, headers=headers[0], json={"difficulty": "easy"}).json()["analysis"] == "真实解析"
    assert client.patch(url, headers=headers[0], json={"analysis": None}).json()["analysis"] is None


def test_asset_schema_disallows_paths_or_identity_reassignment(files_api):
    client, _session, _files, doc, users, headers = files_api
    question = QuestionService(_session).create_question(doc.course_id, "SHORT_ANSWER", "问题", created_by=users[0].id)
    response = client.post(f"/api/questions/{question.id}/assets", headers=headers[0], json={"asset_type": "figure", "file_id": "bad", "file_path": "C:/outside.png"})
    assert response.status_code == 422
    bad = client.post(f"/api/questions/{question.id}/assets/upload", headers=headers[0], files={"file": ("broken.png", b"not image", "image/png")})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "IMAGE_INVALID"
