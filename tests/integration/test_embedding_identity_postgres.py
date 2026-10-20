"""T214 真实 Test PostgreSQL：索引身份、摄取事务与受控接纳。

全部用例只使用显式 Test profile 创建的随机 owned schema；不读取 Development/Demo
数据，不调用模型，不重建索引。Fake 仅替代 Embedding 计算，数据库事务/行锁/约束由
真实 PostgreSQL 执行。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.ingestion.service import IngestionService, StatusListener
from backend.app.ai.retrieval.base import RetrievalFilters
from backend.app.ai.retrieval.vector_search import VectorSearchRetriever
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import (
    Document,
    DocumentChunk,
    KnowledgeBase,
    Role,
    User,
)
from backend.app.models.document_chunk import EMBEDDING_VECTOR_DIMENSION
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService, CourseSummary
from backend.app.services.knowledge_base_service import KnowledgeBaseService
from backend.app.services.settings.index_identity import (
    ADOPTION_WRITE_FAILED,
    EMBEDDING_INCOMPATIBLE,
    IDENTITY_CONFLICT,
    INDEX_BUSY,
    LEGACY_ADOPTED,
    LEGACY_UNKNOWN,
    NATIVE,
    IndexIdentityError,
    IndexIdentityService,
    SqlAlchemyIndexRepository,
)
from tests.postgres_helpers import isolated_postgres_engine

LESSON_TEXT = "第一节 索引身份。\n\n真实数据库验证向量身份与事务。\n".encode()


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 17, 4, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: Any) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


class _Principal:
    def __init__(self, environment_id: UUID) -> None:
        self._facts = {
            "principal_kind": "container_operator",
            "principal_id": "t214-test-operator",
            "environment_id": str(environment_id),
            "instance_id": "t214-test-instance",
            "management_session_id": "t214-test-session",
        }

    def current(self) -> dict[str, Any]:
        return dict(self._facts)

    def session_id(self) -> str:
        return self._facts["management_session_id"]


class _StubEmbedding(BaseEmbeddingProvider):
    provider_name = "t214"

    def __init__(
        self,
        *,
        model_id: str = "t214-embedding-a",
        dimension: int = EMBEDDING_VECTOR_DIMENSION,
    ) -> None:
        self.model_name = model_id
        self.dimension = dimension
        self.calls = 0

    def semantic_parameters(self) -> dict[str, Any]:
        return {
            "normalize": True,
            "document_prefix": "",
            "query_prefix": "",
        }

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        self.calls += len(normalized)
        return self.validate_document_vectors(
            normalized, [self._vector(item) for item in normalized]
        )

    async def embed_query(self, query: str) -> list[float]:
        return self._vector(self.ensure_query(query))

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        dimension = self.dimension
        assert dimension is not None
        return [digest[index % len(digest)] / 255.0 for index in range(dimension)]


def _identity(provider: BaseEmbeddingProvider) -> dict[str, Any]:
    return provider.index_identity(dimension=EMBEDDING_VECTOR_DIMENSION)


def _evidence(kb_id: UUID, identity: dict[str, Any], *, suffix: str = "a") -> dict[str, Any]:
    return {
        "kind": "ingestion_record",
        "source_reference": f"t214/ingestion/{suffix}",
        "source_date": "2025-06-01",
        "kb_scope": str(kb_id),
        "safe_excerpt": "合成脱敏摘录：原摄取记录显示同一模型与语义参数。",
        "supported_fields": {
            "provider": identity["provider"],
            "adapter": identity["adapter"],
            "model_id": identity["model_id"],
            "dimension": identity["dimension"],
            "semantic_parameters": dict(identity["semantic_parameters"]),
        },
    }


def _attestation() -> dict[str, Any]:
    return {
        "verified": True,
        "covers": "已核对原摄取记录及该知识库全部 chunk",
    }


def _preview(
    service: IndexIdentityService,
    *,
    kb_id: UUID,
    identity: dict[str, Any],
    principal: _Principal,
) -> dict[str, Any]:
    return service.preview_adoption(
        kb_id=kb_id,
        expected_identity_revision=None,
        proposed_identity=identity,
        evidence=[_evidence(kb_id, identity)],
        operator_attestation=_attestation(),
        management_session_id=principal.session_id(),
    )


def _confirm(
    service: IndexIdentityService,
    preview: dict[str, Any],
    principal: _Principal,
    *,
    confirmation_id: UUID | None = None,
) -> dict[str, Any]:
    return service.confirm_adoption(
        preview_id=preview["preview_id"],
        expected_identity_revision=preview["expected_identity_revision"],
        confirmation_id=confirmation_id or uuid4(),
        management_session_id=principal.session_id(),
        confirm=True,
    )


def _teacher_role(session: Session) -> Role:
    """角色是 schema 级共享事实：已有教师角色就复用，不重复插入。"""

    existing = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
    if existing is not None:
        return existing
    return Role(name=UserRole.TEACHER, description="教师")


def _seed(
    session: Session, *, legacy_chunk: bool = False
) -> tuple[User, CourseSummary, KnowledgeBase, Document, KnowledgeBaseService]:
    teacher = User(
        username=f"t214-{uuid4().hex[:12]}",
        email=f"t214-{uuid4().hex[:12]}@example.com",
        password_hash=hash_password("正确密码"),
    )
    teacher.roles.append(_teacher_role(session))
    session.add(teacher)
    session.commit()
    course = CourseService(session).create_course(name="T214 课程", created_by=teacher.id)
    service = KnowledgeBaseService(session)
    kb = service.create_knowledge_base(
        course_id=course.id,
        name="T214 知识库",
        teacher_id=teacher.id,
    )
    document = service.upload_document(
        course_id=course.id,
        knowledge_base_id=kb.id,
        uploaded_by=teacher.id,
        original_filename="t214.txt",
        teacher_id=teacher.id,
    )
    loaded_kb = session.get(KnowledgeBase, UUID(kb.id))
    loaded_document = session.get(Document, UUID(document.id))
    assert loaded_kb is not None and loaded_document is not None
    if legacy_chunk:
        loaded_document.status = DocumentStatus.READY
        session.add(
            DocumentChunk(
                document_id=loaded_document.id,
                course_id=course.id,
                knowledge_base_id=loaded_kb.id,
                chunk_index=0,
                content="历史片段",
                embedding=[0.125] * EMBEDDING_VECTOR_DIMENSION,
                chunk_metadata={"source": "legacy"},
            )
        )
        session.commit()
    return teacher, course, loaded_kb, loaded_document, service


def _factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def _chunk_fingerprint(session: Session, kb_id: UUID) -> tuple[Any, ...]:
    rows = session.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.content,
            DocumentChunk.embedding,
        )
        .where(DocumentChunk.knowledge_base_id == kb_id)
        .order_by(DocumentChunk.chunk_index, DocumentChunk.id)
    ).all()
    return tuple(
        (
            str(chunk_id),
            content,
            tuple(float(value) for value in embedding or ()),
        )
        for chunk_id, content, embedding in rows
    )


def _ingest(
    session: Session,
    document_id: UUID,
    provider: BaseEmbeddingProvider,
    *,
    teacher_id: UUID,
) -> Any:
    def factory(listener: StatusListener) -> IngestionService:
        return IngestionService(embedding_provider=provider, on_transition=listener)

    return KnowledgeBaseService(session).ingest_document(
        document_id,
        content=LESSON_TEXT,
        teacher_id=teacher_id,
        ingestion_service_factory=factory,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    with isolated_postgres_engine() as active:
        yield active


def test_first_ingestion_commits_native_identity_with_vectors(engine: Engine) -> None:
    with Session(engine) as session:
        teacher, _, kb, document, _ = _seed(session)
        observer: list[tuple[Any, int]] = []
        document_id = document.id

        @event.listens_for(session, "after_commit")
        def observe(_session: Session) -> None:
            if document.status is not DocumentStatus.READY:
                return
            with Session(engine) as other:
                stored_kb = other.get(KnowledgeBase, kb.id)
                count = other.scalar(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                )
                observer.append(
                    (
                        stored_kb.embedding_identity if stored_kb is not None else None,
                        int(count or 0),
                    )
                )

        result = _ingest(session, document.id, _StubEmbedding(), teacher_id=teacher.id)
        assert result.status is DocumentStatus.READY
        assert result.chunk_count >= 1
        assert observer
        identity, count = observer[-1]
        assert identity is not None and identity["status"] == NATIVE
        assert identity["model_id"] == "t214-embedding-a"
        assert identity["dimension"] == EMBEDDING_VECTOR_DIMENSION
        assert count == result.chunk_count


def test_legacy_unknown_with_vectors_blocks_ingestion_without_deleting_vectors(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        teacher, _, kb, document, _ = _seed(session, legacy_chunk=True)
        before = _chunk_fingerprint(session, kb.id)
        result = _ingest(session, document.id, _StubEmbedding(), teacher_id=teacher.id)
        assert result.status is DocumentStatus.FAILED
        assert result.error_code == EMBEDDING_INCOMPATIBLE
        stored = session.get(KnowledgeBase, kb.id)
        assert stored is not None and stored.embedding_identity is None
        assert _chunk_fingerprint(session, kb.id) == before


def test_adoption_atomically_saves_identity_adoption_and_audit(engine: Engine) -> None:
    with Session(engine) as session:
        _, _, kb, _, _ = _seed(session, legacy_chunk=True)
        before = _chunk_fingerprint(session, kb.id)
        principal = _Principal(uuid4())
        service = IndexIdentityService(
            repository=SqlAlchemyIndexRepository(session),
            clock=_Clock(),
            principal=principal,
        )
        assert service.view(kb_id=kb.id)["status"] == LEGACY_UNKNOWN
        preview = _preview(service, kb_id=kb.id, identity=_identity(_StubEmbedding()), principal=principal)
        confirmed = _confirm(service, preview, principal)
        assert confirmed["status"] == LEGACY_ADOPTED
        stored = session.get(KnowledgeBase, kb.id)
        assert stored is not None and stored.embedding_identity is not None
        assert stored.embedding_identity["status"] == LEGACY_ADOPTED
        adoption = stored.embedding_identity["adoption"]
        assert adoption["previous_status"] == LEGACY_UNKNOWN
        assert adoption["operator"]["principal_id"] == "t214-test-operator"
        assert stored.embedding_identity["audits"]
        assert _chunk_fingerprint(session, kb.id) == before


def test_adoption_commit_failure_rolls_back_identity_audit_and_vectors(engine: Engine) -> None:
    with Session(engine) as session:
        _, _, kb, _, _ = _seed(session, legacy_chunk=True)
        before = _chunk_fingerprint(session, kb.id)
        principal = _Principal(uuid4())
        service = IndexIdentityService(
            repository=SqlAlchemyIndexRepository(session),
            clock=_Clock(),
            principal=principal,
        )
        preview = _preview(service, kb_id=kb.id, identity=_identity(_StubEmbedding()), principal=principal)

        def fail_adoption(_session: Session) -> None:
            stored = _session.get(KnowledgeBase, kb.id)
            if (
                stored is not None
                and stored.embedding_identity is not None
                and stored.embedding_identity.get("status") == LEGACY_ADOPTED
            ):
                raise SQLAlchemyError("synthetic adoption commit failure")

        event.listen(session, "before_commit", fail_adoption)
        try:
            with pytest.raises(IndexIdentityError) as error:
                _confirm(service, preview, principal)
        finally:
            event.remove(session, "before_commit", fail_adoption)
        assert error.value.code == ADOPTION_WRITE_FAILED
        with Session(engine) as other:
            stored = other.get(KnowledgeBase, kb.id)
            assert stored is not None and stored.embedding_identity is None
            assert _chunk_fingerprint(other, kb.id) == before


def test_preview_is_invalidated_by_new_ingestion(engine: Engine) -> None:
    with Session(engine) as session:
        _, _, kb, _, _ = _seed(session, legacy_chunk=True)
        principal = _Principal(uuid4())
        service = IndexIdentityService(
            repository=SqlAlchemyIndexRepository(session),
            clock=_Clock(),
            principal=principal,
        )
        preview = _preview(service, kb_id=kb.id, identity=_identity(_StubEmbedding()), principal=principal)
        with Session(engine) as other:
            other.add(
                DocumentChunk(
                    document_id=session.scalar(
                        select(DocumentChunk.document_id).where(
                            DocumentChunk.knowledge_base_id == kb.id
                        )
                    ),
                    course_id=kb.course_id,
                    knowledge_base_id=kb.id,
                    chunk_index=1,
                    content="新摄取片段",
                    embedding=[0.25] * EMBEDDING_VECTOR_DIMENSION,
                    chunk_metadata={"source": "late-ingestion"},
                )
            )
            other.commit()
        with pytest.raises(IndexIdentityError) as error:
            _confirm(service, preview, principal)
        assert error.value.code == INDEX_BUSY
        stored = session.get(KnowledgeBase, kb.id)
        assert stored is not None and stored.embedding_identity is None


def test_repeated_confirmation_reads_saved_adoption_without_second_audit(engine: Engine) -> None:
    with Session(engine) as session:
        _, _, kb, _, _ = _seed(session, legacy_chunk=True)
        principal = _Principal(uuid4())
        service = IndexIdentityService(
            repository=SqlAlchemyIndexRepository(session),
            clock=_Clock(),
            principal=principal,
        )
        preview = _preview(service, kb_id=kb.id, identity=_identity(_StubEmbedding()), principal=principal)
        confirmation = uuid4()
        first = _confirm(service, preview, principal, confirmation_id=confirmation)
        second = _confirm(service, preview, principal, confirmation_id=confirmation)
        assert first["status"] == second["status"] == LEGACY_ADOPTED
        stored = session.get(KnowledgeBase, kb.id)
        assert stored is not None and stored.embedding_identity is not None
        assert len(stored.embedding_identity["audits"]) == 1
        assert (
            stored.embedding_identity["adoption"]["confirmation_id"]
            == str(confirmation)
        )


def test_compatible_ingestion_is_allowed_after_adoption_and_different_model_is_refused(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        teacher, _, kb, document, _ = _seed(session, legacy_chunk=True)
        principal = _Principal(uuid4())
        service = IndexIdentityService(
            repository=SqlAlchemyIndexRepository(session),
            clock=_Clock(),
            principal=principal,
        )
        preview = _preview(service, kb_id=kb.id, identity=_identity(_StubEmbedding()), principal=principal)
        _confirm(service, preview, principal)
        compatible = _ingest(
            session, document.id, _StubEmbedding(), teacher_id=teacher.id
        )
        assert compatible.status is DocumentStatus.READY
        after = _chunk_fingerprint(session, kb.id)
        incompatible = _ingest(
            session,
            document.id,
            _StubEmbedding(model_id="t214-embedding-b"),
            teacher_id=teacher.id,
        )
        assert incompatible.status is DocumentStatus.FAILED
        assert incompatible.error_code == EMBEDDING_INCOMPATIBLE
        assert _chunk_fingerprint(session, kb.id) == after
        stored = session.get(KnowledgeBase, kb.id)
        assert stored is not None and stored.embedding_identity is not None
        assert stored.embedding_identity["status"] == LEGACY_ADOPTED


def test_vector_query_requires_matching_identity_and_rejects_legacy_unknown(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        teacher, _, kb, document, _ = _seed(session)
        provider = _StubEmbedding()
        assert (
            _ingest(session, document.id, provider, teacher_id=teacher.id).status
            is DocumentStatus.READY
        )
        query = provider._vector("查询")
        filters = RetrievalFilters(knowledge_base_ids=(kb.id,))
        result = VectorSearchRetriever(index_identity=_identity(provider)).search(
            session, query, top_k=5, filters=filters
        )
        assert result
        with pytest.raises(IndexIdentityError) as incompatible:
            VectorSearchRetriever(
                index_identity=_identity(_StubEmbedding(model_id="t214-embedding-b"))
            ).search(session, query, top_k=5, filters=filters)
        assert incompatible.value.code == EMBEDDING_INCOMPATIBLE

        legacy_session = Session(engine)
        try:
            _, _, legacy_kb, _, _ = _seed(legacy_session, legacy_chunk=True)
            with pytest.raises(IndexIdentityError) as legacy:
                VectorSearchRetriever(index_identity=_identity(provider)).search(
                    legacy_session,
                    query,
                    top_k=5,
                    filters=RetrievalFilters(knowledge_base_ids=(legacy_kb.id,)),
                )
            assert legacy.value.code == EMBEDDING_INCOMPATIBLE
        finally:
            legacy_session.close()


def test_concurrent_confirms_only_one_adoption_commits(engine: Engine) -> None:
    factory = _factory(engine)
    with factory() as seed_session:
        _, _, kb, _, _ = _seed(seed_session, legacy_chunk=True)
        kb_id = kb.id
    principal = _Principal(uuid4())
    previews: list[dict[str, Any]] = []
    services: list[IndexIdentityService] = []
    sessions = [factory() for _ in range(2)]
    try:
        for setup in sessions:
            service = IndexIdentityService(
                repository=SqlAlchemyIndexRepository(setup),
                clock=_Clock(),
                principal=principal,
            )
            previews.append(
                _preview(
                    service,
                    kb_id=kb_id,
                    identity=_identity(_StubEmbedding()),
                    principal=principal,
                )
            )
            services.append(service)
        barrier = threading.Barrier(2)

        def worker(index: int) -> Any:
            barrier.wait()
            try:
                return _confirm(
                    services[index],
                    previews[index],
                    principal,
                    confirmation_id=uuid4(),
                )
            except BaseException as error:  # noqa: BLE001 - 并发结果用于断言
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(worker, index) for index in range(2)]
            outcomes = [future.result() for future in results]
    finally:
        for setup in sessions:
            setup.close()
    errors = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(errors) == 1
    assert getattr(errors[0], "code", None) == IDENTITY_CONFLICT
    with factory() as verify:
        stored = verify.get(KnowledgeBase, kb_id)
        assert stored is not None and stored.embedding_identity is not None
        assert stored.embedding_identity["status"] == LEGACY_ADOPTED
        assert len(stored.embedding_identity["audits"]) == 1


def test_concurrent_adoption_and_ingestion_do_not_mix(engine: Engine) -> None:
    factory = _factory(engine)
    with factory() as seed_session:
        teacher, _, kb, document, _ = _seed(seed_session, legacy_chunk=True)
        kb_id, document_id, teacher_id = kb.id, document.id, teacher.id
    before: tuple[Any, ...]
    with factory() as inspect_session:
        before = _chunk_fingerprint(inspect_session, kb_id)
    principal = _Principal(uuid4())
    setup_session = factory()
    try:
        service = IndexIdentityService(
            repository=SqlAlchemyIndexRepository(setup_session),
            clock=_Clock(),
            principal=principal,
        )
        preview = _preview(
            service,
            kb_id=kb_id,
            identity=_identity(_StubEmbedding()),
            principal=principal,
        )
        barrier = threading.Barrier(2)

        def confirm_worker() -> Any:
            barrier.wait()
            try:
                return _confirm(service, preview, principal, confirmation_id=uuid4())
            except BaseException as error:  # noqa: BLE001
                return error

        def ingest_worker() -> Any:
            barrier.wait()
            try:
                with factory() as session:
                    result = _ingest(
                        session,
                        document_id,
                        _StubEmbedding(),
                        teacher_id=teacher_id,
                    )
                    return result
            except BaseException as error:  # noqa: BLE001
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [
                future.result()
                for future in (pool.submit(confirm_worker), pool.submit(ingest_worker))
            ]
    finally:
        setup_session.close()
    with factory() as verify:
        stored = verify.get(KnowledgeBase, kb_id)
        assert stored is not None and stored.embedding_identity is not None
        identity = stored.embedding_identity
        # 行锁把两个操作串行化：接纳要么在摄取被拒后成功，要么先于兼容摄取提交。
        assert identity["status"] == LEGACY_ADOPTED
        assert len(identity["audits"]) == 1
        assert identity["adoption"]["operator"]["principal_id"] == "t214-test-operator"
        fingerprint = _chunk_fingerprint(verify, kb_id)
    adoption, ingestion = outcomes
    assert isinstance(adoption, dict)
    assert adoption["status"] == LEGACY_ADOPTED
    if isinstance(ingestion, BaseException):
        # 摄取先取得行锁：未知身份叠加既有向量被拒绝，旧向量零改动。
        assert getattr(ingestion, "error_code", None) == EMBEDDING_INCOMPATIBLE
        assert fingerprint == before
    else:
        # 接纳先提交：摄取按已接纳身份整体替换片段，不产生半成品或混写。
        assert ingestion.status is DocumentStatus.READY
        assert ingestion.chunk_count == len(fingerprint)
        assert fingerprint and fingerprint != before
