"""T161 authenticated API production and durable independent confirmations; TCR §16."""

from uuid import uuid4

from backend.app.domain.enums import DocumentStatus
from backend.app.models import Course, Document, DocumentChunk, KnowledgeBase
from tests.contract.test_knowledge_base_upload_contract import (
    TEACHER_PASSWORD,
    _login,
)
from tests.contract.test_knowledge_base_upload_contract import (
    client as scope_client,
)
from tests.contract.test_knowledge_base_upload_contract import (
    session_factory as scope_session_factory,
)
from tests.contract.test_knowledge_base_upload_contract import (
    teacher_id as scope_teacher_id,
)

client = scope_client
session_factory = scope_session_factory
teacher_id = scope_teacher_id


def test_chapter_scope_http_boundary_and_real_actor(
    client, session_factory, teacher_id
):
    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    with session_factory() as session:
        course = Course(name="scoped", created_by=teacher_id)
        session.add(course)
        session.flush()
        kb = KnowledgeBase(course_id=course.id, name="scope")
        session.add(kb)
        session.flush()
        document = Document(
            course_id=course.id,
            knowledge_base_id=kb.id,
            uploaded_by=teacher_id,
            original_filename="scope.md",
            file_format="md",
            status=DocumentStatus.READY,
        )
        session.add(document)
        session.flush()
        chunk = DocumentChunk(
            course_id=course.id,
            knowledge_base_id=kb.id,
            document_id=document.id,
            content="真实完整片段",
            chunk_index=0,
            chunk_metadata={"legacy": "original"},
        )
        session.add(chunk)
        session.commit()
        course_id, document_id, chunk_id = (
            str(course.id),
            str(document.id),
            str(chunk.id),
        )
    path = f"/api/knowledge-bases/courses/{course_id}/chapters"
    assert client.post(path, json={"title": "first"}).status_code == 401
    forged = client.post(
        path, headers=headers, json={"title": "first", "confirmed_by": str(uuid4())}
    )
    assert forged.status_code == 422
    created = client.post(
        path,
        headers=headers,
        json={"title": "first", "sections": [{"section_order": 1, "title": "one"}]},
    )
    assert created.status_code == 201, created.text
    chapter = created.json()
    assert chapter["confirmed_by"] == str(teacher_id)
    chunk_path = f"/api/knowledge-bases/chunks/{chunk_id}/scope"
    located = client.patch(
        chunk_path,
        headers=headers,
        json={"chapter_id": chapter["id"], "section_order": 1},
    )
    assert located.status_code == 200, located.text
    tagged = client.patch(
        chunk_path, headers=headers, json={"knowledge_points": [" A ", "A"]}
    )
    assert tagged.status_code == 200, tagged.text
    fresh = client.get(
        f"/api/knowledge-bases/documents/{document_id}/chunks", headers=headers
    )
    assert fresh.status_code == 200, fresh.text
    result = fresh.json()[0]
    assert result["chapter_id"] == chapter["id"] and result["section_order"] == 1
    assert result["metadata"]["knowledge_points"] == ["A"]
    assert result["metadata"]["legacy"] == "original"
    assert result["metadata"]["scope_confirmation"]["knowledge_points"][
        "confirmed_by"
    ] == str(teacher_id)
    assert (
        client.delete(
            f"/api/knowledge-bases/chapters/{chapter['id']}", headers=headers
        ).status_code
        == 409
    )
    assert (
        client.patch(chunk_path, headers=headers, json={"chapter_id": None}).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/knowledge-bases/chapters/{chapter['id']}", headers=headers
        ).status_code
        == 204
    )


def test_chapter_scope_rejects_other_teacher_course(
    client, session_factory, teacher_id
):
    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    with session_factory() as session:
        from backend.app.models import User

        other = User(
            username="other", email="other@scope.invalid", password_hash="unused"
        )
        session.add(other)
        session.flush()
        course = Course(name="private", created_by=other.id)
        session.add(course)
        session.commit()
        course_id = str(course.id)
    response = client.post(
        f"/api/knowledge-bases/courses/{course_id}/chapters",
        headers=headers,
        json={"title": "forged"},
    )
    assert response.status_code == 403
