"""T039 资料上传摄取 API 契约测试：multipart 真实文件内容与真实处理状态。

测试请求边界使用 SQLite，成功摄取使用隔离 PostgreSQL；Embedding Provider 使用替身。
上传端点必须接收真实文件正文并触发摄取：成功时返回 Ready，未就绪或解析失败时返回
具体失败原因，不得因为“上传成功”就返回 Ready。

TCR（2026-09-14，H02）：补充 API 禁止 Ready/Uploaded/Chunking 的反向契约，
以实际请求和数据库状态验证拒绝行为；摄取成功断言依赖真实 PostgreSQL 全文索引。
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator, Sequence
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from uuid import UUID

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.ingestion.service import IngestionService, StatusListener
from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import Document, Role, User
from backend.app.models.document_chunk import EMBEDDING_VECTOR_DIMENSION
from backend.app.services import knowledge_base_service as knowledge_base_module
from backend.app.services.auth_service import hash_password
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.settings_helpers import build_test_settings

TEST_JWT_SECRET = token_urlsafe(48)
TEACHER_PASSWORD = token_urlsafe(24)
STUDENT_PASSWORD = token_urlsafe(24)

LESSON_TEXT = "第一节 课程资料。\n\n检索增强生成结合检索与生成。\n".encode()

UPLOAD_PATH = "/api/knowledge-bases/{knowledge_base_id}/documents/upload"


class StubEmbeddingProvider(BaseEmbeddingProvider):
    """可控的 Embedding 替身：确定性 1024 维向量，可模拟未就绪状态。"""

    provider_name = "stub"
    model_name = "stub-1024"

    def __init__(self, *, ready: bool = True, detail: str | None = None) -> None:
        self.dimension = EMBEDDING_VECTOR_DIMENSION
        self._ready = ready
        self._readiness_detail = detail

    def is_ready(self) -> bool:
        return self._ready

    def readiness_detail(self) -> str | None:
        return self._readiness_detail

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        return self.validate_document_vectors(
            normalized, [self._vector(text) for text in normalized]
        )

    async def embed_query(self, query: str) -> list[float]:
        return self._vector(self.ensure_query(query))

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index % len(digest)] / 255 for index in range(self.dimension)]


@pytest.fixture
def session_factory(request: pytest.FixtureRequest) -> Generator[sessionmaker[Session], None, None]:
    """成功摄取使用真实 PostgreSQL，其余请求边界使用隔离 SQLite。"""

    if getattr(request, "param", None) == "postgres":
        with isolated_postgres_engine() as engine:
            yield sessionmaker(bind=engine, expire_on_commit=False)
        return
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
) -> Generator[TestClient, None, None]:
    """构造使用隔离数据库和测试 JWT 密钥的 API 客户端。"""

    with gr.Blocks() as test_gradio_app:
        gr.Markdown("资料上传契约测试")
    application = create_app(
        settings=build_test_settings(JWT_SECRET_KEY=TEST_JWT_SECRET),
        gradio_app=test_gradio_app,
    )

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            try:
                yield session
            except Exception:
                session.rollback()
                raise

    application.dependency_overrides[get_db] = override_get_db
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()


def _add_user(
    session_factory: sessionmaker[Session],
    *,
    username: str,
    role: UserRole,
    password: str,
) -> UUID:
    """写入具备指定角色的测试账号。"""

    with session_factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password(password),
        )
        user.roles.append(Role(name=role, description=str(role)))
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


@pytest.fixture
def teacher_id(session_factory: sessionmaker[Session]) -> UUID:
    """写入教师账号。"""

    return _add_user(
        session_factory,
        username="upload-teacher",
        role=UserRole.TEACHER,
        password=TEACHER_PASSWORD,
    )


@pytest.fixture
def student_id(session_factory: sessionmaker[Session]) -> UUID:
    """写入学生账号，用于验证权限边界。"""

    return _add_user(
        session_factory,
        username="upload-student",
        role=UserRole.STUDENT,
        password=STUDENT_PASSWORD,
    )


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    """登录并返回 Bearer 头。"""

    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_knowledge_base(client: TestClient, headers: dict[str, str]) -> str:
    """创建课程与知识库，返回知识库标识。"""

    course = client.post(
        "/api/courses",
        headers=headers,
        json={"name": "资料上传课程", "description": "用于校验真实文件摄取。"},
    )
    assert course.status_code == 201, course.text
    knowledge_base = client.post(
        "/api/knowledge-bases",
        headers=headers,
        json={"course_id": course.json()["id"], "name": "课程资料"},
    )
    assert knowledge_base.status_code == 201, knowledge_base.text
    return knowledge_base.json()["id"]


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: BaseEmbeddingProvider) -> None:
    """用替身 Provider 替换默认编排器工厂，保持阶段回调。"""

    def factory(listener: StatusListener) -> IngestionService:
        return IngestionService(embedding_provider=provider, on_transition=listener)

    monkeypatch.setattr(
        knowledge_base_module,
        "_default_ingestion_service_factory",
        factory,
    )


@pytest.mark.parametrize("session_factory", ["postgres"], indirect=True)
def test_upload_document_ingests_real_content_and_returns_ready(
    client: TestClient,
    session_factory: sessionmaker[Session],
    teacher_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传真实 TXT：内容被接收并落盘，摄取完成后返回 Ready 与片段数量。"""

    _install_provider(monkeypatch, StubEmbeddingProvider())
    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    knowledge_base_id = _create_knowledge_base(client, headers)

    response = client.post(
        UPLOAD_PATH.format(knowledge_base_id=knowledge_base_id),
        headers=headers,
        files={"file": ("第一章讲义.txt", LESSON_TEXT, "text/plain")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == DocumentStatus.READY.value
    assert body["chunk_count"] >= 1
    assert body["error_code"] is None
    assert body["retryable"] is False

    with session_factory() as session:
        document = session.get(Document, UUID(body["document_id"]))
        assert document is not None
        assert document.status is DocumentStatus.READY
        assert document.original_filename == "第一章讲义.txt"
        assert document.file_format == "txt"
        # 真实文件内容已落盘，后续重新处理可以再次读取同一份资料。
        assert document.storage_path is not None
        stored = Path(document.storage_path)
        assert stored.is_file()
        assert stored.read_bytes() == LESSON_TEXT

    detail = client.get(
        f"/api/knowledge-bases/{knowledge_base_id}/documents/{body['document_id']}",
        headers=headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == DocumentStatus.READY.value


def test_upload_document_reports_not_ready_provider(
    client: TestClient,
    session_factory: sessionmaker[Session],
    teacher_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedding Provider 未就绪时返回 Failed 与具体原因，不伪造 Ready。"""

    _install_provider(
        monkeypatch,
        StubEmbeddingProvider(ready=False, detail="缺少 EMBEDDING_MODEL 配置"),
    )
    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    knowledge_base_id = _create_knowledge_base(client, headers)

    response = client.post(
        UPLOAD_PATH.format(knowledge_base_id=knowledge_base_id),
        headers=headers,
        files={"file": ("讲义.txt", LESSON_TEXT, "text/plain")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == DocumentStatus.FAILED.value
    assert body["error_code"] == "EMBEDDING_PROVIDER_NOT_READY"
    assert body["error_message"]
    assert body["retryable"] is False
    assert body["chunk_count"] == 0

    with session_factory() as session:
        document = session.get(Document, UUID(body["document_id"]))
        assert document is not None
        assert document.status is DocumentStatus.FAILED
        assert document.error_message


def test_upload_document_reports_empty_file_failure(
    client: TestClient,
    teacher_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空文件按真实解析失败处理（DOCUMENT_EMPTY），并保持不可重试。"""

    _install_provider(monkeypatch, StubEmbeddingProvider())
    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    knowledge_base_id = _create_knowledge_base(client, headers)

    response = client.post(
        UPLOAD_PATH.format(knowledge_base_id=knowledge_base_id),
        headers=headers,
        files={"file": ("空文件.txt", b"", "text/plain")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == DocumentStatus.FAILED.value
    assert body["error_code"] == "DOCUMENT_EMPTY"
    assert body["retryable"] is False


def test_upload_document_rejects_unsupported_format(
    client: TestClient,
    teacher_id: UUID,
) -> None:
    """不支持的格式在元数据阶段被拒绝，返回 422 而不是创建不可用资料。"""

    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    knowledge_base_id = _create_knowledge_base(client, headers)

    response = client.post(
        UPLOAD_PATH.format(knowledge_base_id=knowledge_base_id),
        headers=headers,
        files={"file": ("讲义.docx", b"binary-content", "application/octet-stream")},
    )

    assert response.status_code == 422, response.text
    assert "PDF" in response.json()["detail"]


def test_upload_document_requires_teacher_permission(
    client: TestClient,
    student_id: UUID,
) -> None:
    """学生不能触发资料摄取。"""

    headers = _login(client, "upload-student", STUDENT_PASSWORD)

    response = client.post(
        UPLOAD_PATH.format(knowledge_base_id="11111111-1111-4111-8111-111111111111"),
        headers=headers,
        files={"file": ("讲义.txt", LESSON_TEXT, "text/plain")},
    )

    assert response.status_code == 403, response.text


def test_upload_document_requires_file_field(
    client: TestClient,
    teacher_id: UUID,
) -> None:
    """缺少 multipart 文件字段时返回 422，避免登记空资料。"""

    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    knowledge_base_id = _create_knowledge_base(client, headers)

    response = client.post(
        UPLOAD_PATH.format(knowledge_base_id=knowledge_base_id),
        headers=headers,
    )

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("session_factory", ["postgres"], indirect=True)
def test_uploaded_chunks_are_listed_with_source_metadata(
    client: TestClient,
    teacher_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摄取后的知识片段可通过文档列表与来源详情追溯到文件与课程。"""

    _install_provider(monkeypatch, StubEmbeddingProvider())
    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    knowledge_base_id = _create_knowledge_base(client, headers)

    upload = client.post(
        UPLOAD_PATH.format(knowledge_base_id=knowledge_base_id),
        headers=headers,
        files={"file": ("讲义.txt", LESSON_TEXT, "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["status"] == DocumentStatus.READY.value

    documents = client.get(
        f"/api/knowledge-bases/{knowledge_base_id}/documents",
        headers=headers,
    )
    assert documents.status_code == 200, documents.text
    listed: list[dict[str, Any]] = documents.json()
    assert [item["original_filename"] for item in listed] == ["讲义.txt"]
    assert listed[0]["status"] == DocumentStatus.READY.value
    assert listed[0]["storage_path"]


@pytest.mark.parametrize(
    ("current_status", "requested_status"),
    [
        (DocumentStatus.EMBEDDING, DocumentStatus.READY),
        (DocumentStatus.UPLOADED, DocumentStatus.UPLOADED),
        (DocumentStatus.PARSING, DocumentStatus.CHUNKING),
    ],
)
def test_status_api_rejects_internal_only_states(
    client: TestClient,
    session_factory: sessionmaker[Session],
    teacher_id: UUID,
    current_status: DocumentStatus,
    requested_status: DocumentStatus,
) -> None:
    """状态 API 仅开放 Parsing、Embedding、Failed，不能伪造摄取成功。"""

    headers = _login(client, "upload-teacher", TEACHER_PASSWORD)
    knowledge_base_id = _create_knowledge_base(client, headers)
    created = client.post(
        f"/api/knowledge-bases/{knowledge_base_id}/documents",
        headers=headers, json={"original_filename": "资料.txt"},
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["id"]
    with session_factory() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        document.status = current_status
        session.commit()

    response = client.patch(
        f"/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/status",
        headers=headers, json={"status": requested_status.value},
    )
    assert response.status_code == 422, response.text
    with session_factory() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.status is current_status
