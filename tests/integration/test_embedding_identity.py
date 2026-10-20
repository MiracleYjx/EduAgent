"""TCR-S5-RUNTIME-SNAPSHOT-INDEX-013：T211 Embedding 索引身份保护与历史证据接纳。

T214（索引语义身份/接纳事务）尚未实现：需要索引身份服务的用例在 call 阶段以
``T214_IMPLEMENTATION_REQUIRED`` 显式红灯；其余用例固定当前已成立的结构事实
（向量维度仍 1024、无并行身份/审计表）与端口替身的负对照。

边界：KB 端口是内存 Fake（只存储事实、提供行锁与故障注入），不连真实 PG/Redis、
不构造真实向量模型、不调用付费模型、不操作 Development/Demo 数据。
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.support import runtime_settings_fakes as t211

_MISSING = object()


@pytest.fixture
def repo() -> t211.FakeKnowledgeBaseRepository:
    return t211.FakeKnowledgeBaseRepository()


@pytest.fixture
def clock() -> t211.FakeClock:
    return t211.FakeClock()


@pytest.fixture
def principal() -> t211.FakePrincipal:
    return t211.FakePrincipal()


def _service(
    repo: t211.FakeKnowledgeBaseRepository, clock: t211.FakeClock, principal: t211.FakePrincipal
) -> Any:
    return t211.index_service(repo, clock=clock, principal=principal)


def _legacy_row(
    repo: t211.FakeKnowledgeBaseRepository, *, chunks: bool = True
) -> t211.FakeKbRow:
    return repo.add(
        chunks={"chunk-legacy-1": t211.synthetic_vector(1)} if chunks else None
    )


def _preview(
    service: Any,
    row: t211.FakeKbRow,
    principal: t211.FakePrincipal,
    *,
    identity: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    expected: Any = _MISSING,
    session: str | None = None,
    attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return t211.preview_adoption(
        service,
        kb_id=row.kb_id,
        expected_identity_revision=row.identity_revision if expected is _MISSING else expected,
        proposed_identity=identity if identity is not None else t211.proposed_identity(),
        evidence=evidence if evidence is not None else [t211.evidence_record(row.kb_id)],
        operator_attestation=attestation if attestation is not None else t211.attestation(),
        management_session_id=session or principal.session_id(),
    )


def _confirm(
    service: Any,
    preview: dict[str, Any],
    principal: t211.FakePrincipal,
    *,
    confirmation: UUID | None = None,
    session: str | None = None,
    confirm: bool = True,
) -> dict[str, Any]:
    return t211.confirm_adoption(
        service,
        preview_id=t211.value_of(preview, "preview_id"),
        expected_identity_revision=t211.value_of(preview, "expected_identity_revision"),
        confirmation_id=confirmation or uuid4(),
        management_session_id=session or principal.session_id(),
        confirm=confirm,
    )


def _errors(results: list[Any]) -> list[BaseException]:
    return [item for item in results if isinstance(item, BaseException)]


# ---------------------------------------------------------------------------
# 结构事实与端口负对照（当前实现即应成立）
# ---------------------------------------------------------------------------


def test_vector_dimension_stays_1024() -> None:
    from backend.app.models.document_chunk import EMBEDDING_VECTOR_DIMENSION

    assert EMBEDDING_VECTOR_DIMENSION == 1024
    assert len(t211.synthetic_vector(1)) == EMBEDDING_VECTOR_DIMENSION


def test_no_parallel_identity_or_audit_table_is_added() -> None:
    import backend.app.models  # noqa: F401  仅用于填充 SQLAlchemy 元数据
    from backend.app.models.base import Base

    tables = {
        name
        for name in Base.metadata.tables
        if any(part in name for part in ("embedding", "identity", "adoption", "index_"))
    }
    assert tables == set()


def test_knowledge_base_exposes_nullable_embedding_identity() -> None:
    from backend.app.models.knowledge_base import KnowledgeBase

    column = KnowledgeBase.__table__.c.get("embedding_identity")
    if column is None:
        pytest.fail(
            "T214_IMPLEMENTATION_REQUIRED: KnowledgeBase.embedding_identity（可空 JSONB）",
            pytrace=False,
        )
    assert column.nullable is True
    assert column.server_default is None
    assert "JSON" in type(column.type).__name__.upper() or "JSON" in str(column.type).upper()


def test_port_row_lock_serializes_writers(repo: t211.FakeKnowledgeBaseRepository) -> None:
    row = repo.add()
    active = 0
    observed: list[int] = []

    def worker() -> None:
        nonlocal active
        with repo.begin(row.kb_id):
            active += 1
            observed.append(active)
            time.sleep(0.02)
            active -= 1

    results = t211.run_concurrently(worker, worker)
    assert _errors(results) == []
    assert observed and max(observed) == 1, "端口行锁必须真实串行化，否则并发用例无意义"


def test_port_returns_raw_facts_without_identity_decisions(
    repo: t211.FakeKnowledgeBaseRepository,
) -> None:
    row = repo.add(chunks={"chunk-legacy-1": t211.synthetic_vector(2)})
    facts = repo.facts(row.kb_id)
    assert facts["identity"] is None and facts["identity_revision"] is None
    assert facts["adoption"] is None and facts["audits"] == [] and facts["commit_count"] == 0
    assert facts["stored_dimension"] == 1024 and facts["chunk_count"] == 1


# ---------------------------------------------------------------------------
# 读取与 legacy_unknown（T214）
# ---------------------------------------------------------------------------


def test_null_identity_reads_as_legacy_unknown_without_write_back(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    view = t211.identity_view(service, row.kb_id)
    assert view["status"] == t211.LEGACY_UNKNOWN
    dimension = view.get("dimension", view.get("stored_dimension"))
    assert dimension == 1024
    facts = repo.facts(row.kb_id)
    assert facts["identity"] is None and facts["commit_count"] == 0, "读取不得写库回填"


def test_legacy_unknown_keeps_model_version_unknown(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    view = t211.identity_view(service, row.kb_id)
    assert view.get("model_version") is None
    assert view.get("model_id") in (None, "")
    assert view.get("adoption") is None
    assert repo.facts(row.kb_id)["identity"] is None


def test_legacy_unknown_requires_adoption_before_new_ingestion(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    with pytest.raises(Exception) as error:
        t211.require_compatible(service, kb_id=row.kb_id, identity=t211.proposed_identity())
    assert t211.safe_code(error.value) == t211.EMBEDDING_INCOMPATIBLE
    assert repo.facts(row.kb_id)["identity"] is None


# ---------------------------------------------------------------------------
# 首次摄取 native 登记（T214）
# ---------------------------------------------------------------------------


def test_first_native_registration_commits_identity_with_vectors(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = repo.add()
    service = _service(repo, clock, principal)
    with repo.begin(row.kb_id) as uow:
        t211.register_native(
            service, kb_id=row.kb_id, identity=t211.proposed_identity(identity_revision=1), unit_of_work=uow
        )
        uow.write_vectors({"chunk-native-1": t211.synthetic_vector(1)})
        uow.commit()
    facts = repo.facts(row.kb_id)
    assert facts["identity"] is not None and facts["identity"]["status"] == t211.NATIVE
    assert facts["chunk_count"] == 1 and facts["commit_count"] == 1
    assert t211.identity_view(service, row.kb_id)["status"] == t211.NATIVE


def test_first_native_registration_is_atomic_on_commit_failure(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = repo.add()
    service = _service(repo, clock, principal)
    repo.fail_next_commit(row.kb_id)
    with repo.begin(row.kb_id) as uow:
        t211.register_native(
            service, kb_id=row.kb_id, identity=t211.proposed_identity(identity_revision=1), unit_of_work=uow
        )
        uow.write_vectors({"chunk-native-1": t211.synthetic_vector(1)})
        with pytest.raises(RuntimeError):
            uow.commit()
    facts = repo.facts(row.kb_id)
    assert facts["identity"] is None, "提交失败不得先落下身份"
    assert facts["chunk_count"] == 0 and facts["commit_count"] == 0


def test_first_native_registration_refuses_existing_vectors_or_identity(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    legacy = _legacy_row(repo)
    native = repo.add(
        identity={**t211.proposed_identity(), "status": t211.NATIVE},
        chunks={"chunk-native-1": t211.synthetic_vector(1)},
    )
    service = _service(repo, clock, principal)
    for row in (legacy, native):
        with repo.begin(row.kb_id) as uow:
            with pytest.raises(Exception) as error:
                t211.register_native(
                    service,
                    kb_id=row.kb_id,
                    identity=t211.proposed_identity(identity_revision=9),
                    unit_of_work=uow,
                )
            assert t211.safe_code(error.value) in t211.CONFLICT_CODES | {t211.EMBEDDING_INCOMPATIBLE}
    assert repo.facts(legacy.kb_id)["identity"] is None, "历史有向量的库不得被覆盖成 native"
    assert repo.facts(native.kb_id)["identity"]["model_id"] == t211.proposed_identity()["model_id"]


# ---------------------------------------------------------------------------
# 语义兼容（T214）
# ---------------------------------------------------------------------------


def _native_row(
    repo: t211.FakeKnowledgeBaseRepository, identity: dict[str, Any]
) -> t211.FakeKbRow:
    return repo.add(identity={**identity, "status": t211.NATIVE}, chunks={"chunk-native-1": t211.synthetic_vector(1)})


def test_same_dimension_different_model_is_incompatible(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _native_row(repo, t211.proposed_identity(model_id="t211-embedding-a"))
    service = _service(repo, clock, principal)
    with pytest.raises(Exception) as error:
        t211.require_compatible(
            service, kb_id=row.kb_id, identity=t211.proposed_identity(model_id="t211-embedding-b")
        )
    assert t211.safe_code(error.value) == t211.EMBEDDING_INCOMPATIBLE


def test_same_model_different_semantic_parameters_is_incompatible(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _native_row(repo, t211.proposed_identity())
    service = _service(repo, clock, principal)
    for changed in (
        {"normalize": False, "document_prefix": ""},
        {"normalize": True, "document_prefix": "passage: "},
    ):
        with pytest.raises(Exception) as error:
            t211.require_compatible(
                service,
                kb_id=row.kb_id,
                identity=t211.proposed_identity(semantic_parameters=changed),
            )
        assert t211.safe_code(error.value) == t211.EMBEDDING_INCOMPATIBLE


def test_identical_semantic_identity_is_compatible_and_carries_no_secret(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    identity = t211.proposed_identity()
    row = _native_row(repo, identity)
    service = _service(repo, clock, principal)
    text = json.dumps(identity, ensure_ascii=False).lower()
    for forbidden in ("api_key", "apikey", "secret", "token", "password"):
        assert forbidden not in text, "身份对象不得承载秘密；密钥轮换不改变向量语义"
    before = repo.facts(row.kb_id)
    t211.require_compatible(service, kb_id=row.kb_id, identity=dict(identity))
    after = repo.facts(row.kb_id)
    assert after["identity"] == before["identity"]
    assert after["commit_count"] == before["commit_count"] == 0


def test_dimension_change_is_rejected_before_any_model_call(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _native_row(repo, t211.proposed_identity())
    service = _service(repo, clock, principal)
    with pytest.raises(Exception) as error:
        t211.require_compatible(
            service, kb_id=row.kb_id, identity=t211.proposed_identity(dimension=768)
        )
    assert t211.safe_code(error.value) == t211.EMBEDDING_INCOMPATIBLE
    assert repo.facts(row.kb_id)["stored_dimension"] == 1024


# ---------------------------------------------------------------------------
# 历史证据接纳：预览（T214）
# ---------------------------------------------------------------------------


def test_preview_requires_evidence(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    with pytest.raises(Exception) as error:
        _preview(service, row, principal, evidence=[])
    assert t211.safe_code(error.value) == t211.EVIDENCE_INSUFFICIENT
    assert repo.facts(row.kb_id)["commit_count"] == 0


def test_preview_rejects_current_settings_or_probe_as_historical_evidence(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    for kind in ("current_settings", "probe_result", "live_probe"):
        with pytest.raises(Exception) as error:
            _preview(
                service,
                row,
                principal,
                evidence=[t211.evidence_record(row.kb_id, kind=kind)],
            )
        assert t211.safe_code(error.value) == t211.EVIDENCE_INSUFFICIENT


def test_preview_reports_conflicting_evidence(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    evidence = [
        t211.evidence_record(row.kb_id, model_id="t211-embedding-a"),
        t211.evidence_record(row.kb_id, model_id="t211-embedding-b"),
    ]
    with pytest.raises(Exception) as error:
        _preview(service, row, principal, evidence=evidence)
    assert t211.safe_code(error.value) == t211.EVIDENCE_CONFLICT


def test_preview_reports_verified_facts_and_unknown_limits(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    verified = t211.facts_of(t211.value_of(preview, "verified_facts"))
    assert verified.get("stored_dimension") == 1024
    accepted = t211.facts_of(t211.value_of(preview, "accepted_identity"))
    assert accepted["model_id"] == "t211-embedding-a"
    assert accepted["model_version"] is None, "未知 model_version 必须保持 null"
    assert t211.value_of(preview, "expires_at")
    assert t211.value_of(preview, "evidence")
    assert repo.facts(row.kb_id)["identity"] is None


def test_preview_expires_after_ten_minutes(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    clock.advance(minutes=11)
    with pytest.raises(Exception) as error:
        _confirm(service, preview, principal)
    assert t211.safe_code(error.value) == t211.PREVIEW_EXPIRED
    assert repo.facts(row.kb_id)["identity"] is None


def test_preview_is_invalidated_by_new_ingestion(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    with repo.begin(row.kb_id) as uow:
        uow.write_vectors({"chunk-late-1": t211.synthetic_vector(7)})
        uow.commit()
    with pytest.raises(Exception) as error:
        _confirm(service, preview, principal)
    assert t211.safe_code(error.value) in t211.CONFLICT_CODES
    assert repo.facts(row.kb_id)["identity"] is None


# ---------------------------------------------------------------------------
# 历史证据接纳：确认与事务（T214）
# ---------------------------------------------------------------------------


def test_confirm_requires_explicit_confirmation(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    with pytest.raises(Exception) as error:
        _confirm(service, preview, principal, confirm=False)
    assert t211.safe_code(error.value)
    facts = repo.facts(row.kb_id)
    assert facts["identity"] is None and facts["adoption"] is None and facts["commit_count"] == 0


def test_confirm_adopts_identity_and_writes_audit(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    confirmed = _confirm(service, preview, principal)
    facts = repo.facts(row.kb_id)
    assert facts["identity"] is not None and facts["identity"]["status"] == t211.LEGACY_ADOPTED
    assert t211.facts_of(t211.value_of(confirmed, "status")) == t211.LEGACY_ADOPTED or (
        confirmed["status"] == t211.LEGACY_ADOPTED
    )
    adoption = t211.value_of(facts, "adoption")
    adoption = t211.facts_of(adoption)
    assert adoption["previous_status"] == t211.LEGACY_UNKNOWN
    accepted = t211.facts_of(t211.value_of(adoption, "accepted_identity"))
    assert accepted["model_id"] == "t211-embedding-a" and accepted["model_version"] is None
    assert t211.value_of(adoption, "adoption_id")
    assert t211.value_of(adoption, "confirmation_id")
    assert t211.value_of(adoption, "accepted_at")
    assert facts["audits"], "身份与审计必须同事务保存"
    assert facts["commit_count"] == 1
    assert facts["writes_without_audit"] == 0


def test_confirm_rejects_stale_expected_identity_revision(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    row.identity_revision = 3  # 其他写入方已改动身份修订
    with pytest.raises(Exception) as error:
        _confirm(service, preview, principal)
    assert t211.safe_code(error.value) == t211.IDENTITY_CONFLICT
    facts = repo.facts(row.kb_id)
    assert facts["adoption"] is None and facts["identity"] is None


def test_repeated_confirmation_id_returns_saved_adoption(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    confirmation = uuid4()
    first = _confirm(service, preview, principal, confirmation=confirmation)
    second = _confirm(service, preview, principal, confirmation=confirmation)
    assert t211.facts_of(first) == t211.facts_of(second)
    assert repo.facts(row.kb_id)["commit_count"] == 1


def test_confirm_write_failure_keeps_legacy_unknown(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    before = repo.facts(row.kb_id)
    repo.fail_next_commit(row.kb_id)
    with pytest.raises(Exception) as error:
        _confirm(service, preview, principal)
    assert t211.safe_code(error.value)
    facts = repo.facts(row.kb_id)
    assert facts["identity"] is None, "审计写失败不得留下已改身份"
    assert facts["adoption"] is None and facts["audits"] == []
    assert facts["commit_count"] == before["commit_count"] == 0
    assert facts["vector_fingerprint"] == before["vector_fingerprint"]


def test_confirm_does_not_touch_chunks_or_dimension(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    before = repo.facts(row.kb_id)
    _confirm(service, _preview(service, row, principal), principal)
    after = repo.facts(row.kb_id)
    assert after["vector_fingerprint"] == before["vector_fingerprint"]
    assert after["chunk_count"] == before["chunk_count"] == 1
    assert after["stored_dimension"] == before["stored_dimension"] == 1024


def test_operator_facts_come_from_server_session(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    hostile = {
        "verified": True,
        "covers": "合成声明",
        "principal_id": "t211-hostile-operator",
        "principal_kind": "local_os",
        "management_session_id": "t211-hostile-session",
    }
    preview = _preview(service, row, principal, attestation=hostile)
    _confirm(service, preview, principal)
    adoption = t211.facts_of(repo.facts(row.kb_id)["adoption"])
    operator = t211.facts_of(t211.value_of(adoption, "operator"))
    assert operator["principal_id"] == principal.facts["principal_id"]
    assert operator["management_session_id"] == principal.session_id()
    assert operator["principal_kind"] in {"local_os", "container_operator"}
    assert "t211-hostile" not in json.dumps(adoption, ensure_ascii=False)


def test_after_adoption_compatible_ingestion_is_allowed_and_incompatible_refused(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    _confirm(service, _preview(service, row, principal), principal)
    accepted = t211.facts_of(t211.value_of(repo.facts(row.kb_id), "identity"))
    compatible = {
        name: accepted[name]
        for name in (
            "provider",
            "adapter",
            "endpoint_identity",
            "model_id",
            "model_version",
            "dimension",
            "semantic_parameters",
            "identity_revision",
        )
    }
    t211.require_compatible(service, kb_id=row.kb_id, identity=compatible)
    with pytest.raises(Exception) as error:
        t211.require_compatible(
            service, kb_id=row.kb_id, identity=t211.proposed_identity(model_id="t211-embedding-z")
        )
    assert t211.safe_code(error.value) == t211.EMBEDDING_INCOMPATIBLE


def test_view_and_preview_are_read_only_and_need_no_provider(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)  # 不注入任何 Provider/Embedding
    before = repo.facts(row.kb_id)
    view = t211.identity_view(service, row.kb_id)
    preview = _preview(service, row, principal)
    after = repo.facts(row.kb_id)
    assert view["status"] == t211.LEGACY_UNKNOWN
    assert t211.value_of(preview, "preview_id")
    assert after["identity"] is None and after["adoption"] is None
    assert after["commit_count"] == before["commit_count"] == 0
    assert after["vector_fingerprint"] == before["vector_fingerprint"]


# ---------------------------------------------------------------------------
# 并发事务保护（T214）
# ---------------------------------------------------------------------------


def test_concurrent_confirm_and_first_ingest_are_mutually_exclusive(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = repo.add(chunks={"chunk-legacy-1": t211.synthetic_vector(3)})
    service = _service(repo, clock, principal)
    preview = _preview(service, row, principal)
    confirmation = uuid4()

    def confirm() -> Any:
        return _confirm(service, preview, principal, confirmation=confirmation)

    def ingest() -> Any:
        with repo.begin(row.kb_id) as uow:
            t211.register_native(
                service,
                kb_id=row.kb_id,
                identity=t211.proposed_identity(identity_revision=1),
                unit_of_work=uow,
            )
            uow.write_vectors({"chunk-native-1": t211.synthetic_vector(4)})
            uow.commit()
        return "ingested"

    results = t211.run_concurrently(confirm, ingest)
    errors = _errors(results)
    facts = repo.facts(row.kb_id)
    assert facts["commit_count"] == 1, "并发下只允许一方提交"
    assert facts["writes_without_audit"] == 0
    assert facts["identity"] is not None
    assert facts["identity"]["status"] in {t211.NATIVE, t211.LEGACY_ADOPTED}
    if facts["identity"]["status"] == t211.LEGACY_ADOPTED:
        assert facts["adoption"] is not None and facts["audits"]
    assert errors, "行锁串行化后落败的一方必须显式报告冲突而不是静默成功"
    for error in errors:
        assert t211.safe_code(error) in t211.CONFLICT_CODES | {t211.EMBEDDING_INCOMPATIBLE}


def test_concurrent_confirms_only_one_writes(
    repo: t211.FakeKnowledgeBaseRepository,
    clock: t211.FakeClock,
    principal: t211.FakePrincipal,
) -> None:
    row = _legacy_row(repo)
    service = _service(repo, clock, principal)
    first = _preview(service, row, principal)
    second = _preview(service, row, principal)
    results = t211.run_concurrently(
        lambda: _confirm(service, first, principal, confirmation=uuid4()),
        lambda: _confirm(service, second, principal, confirmation=uuid4()),
    )
    errors = _errors(results)
    facts = repo.facts(row.kb_id)
    assert len(errors) <= 1
    assert facts["commit_count"] == 1 and facts["writes_without_audit"] == 0
    assert facts["identity"] is not None and facts["identity"]["status"] == t211.LEGACY_ADOPTED
    assert facts["audits"]
    for error in errors:
        assert t211.safe_code(error) in t211.CONFLICT_CODES | {t211.PREVIEW_EXPIRED}
