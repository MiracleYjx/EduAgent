"""Embedding 索引语义身份与历史接纳（T214）。

边界与责任：

- ``KnowledgeBase.embedding_identity`` 是唯一身份事实源；历史 NULL 只读映射为
  ``legacy_unknown``，不写库回填，也不以“当前配置”冒充历史来源。
- 新知识库首次成功摄取在同一数据库事务中写 ``native`` 身份与向量；身份写入失败
  不得留下半成品片段。
- 历史库只有操作者提供可核验证据、显式确认后，才原子保存 ``legacy_adopted``
  身份、接纳事实与审计；不修改 chunk、向量或维度。
- 查询与新增摄取按 provider/adapter/endpoint/model/version/dimension/语义参数
  判定兼容；同维度不同模型或不同语义参数同样拒绝。

本模块不调用 Embedding/LLM，不下载模型，不读取当前 active 配置，不操作任何非
Test 数据库。API/UI 接线分别归 T220/T228。
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.app.models.document_chunk import (
    EMBEDDING_VECTOR_DIMENSION,
    DocumentChunk,
)
from backend.app.models.knowledge_base import KnowledgeBase

NATIVE: Final[str] = "native"
LEGACY_UNKNOWN: Final[str] = "legacy_unknown"
LEGACY_ADOPTED: Final[str] = "legacy_adopted"
IDENTITY_STATUSES: Final[frozenset[str]] = frozenset(
    {NATIVE, LEGACY_UNKNOWN, LEGACY_ADOPTED}
)

INDEX_IDENTITY_NOT_FOUND: Final[str] = "INDEX_IDENTITY_NOT_FOUND"
INDEX_IDENTITY_INVALID: Final[str] = "INDEX_IDENTITY_INVALID"
EMBEDDING_INCOMPATIBLE: Final[str] = "EMBEDDING_INCOMPATIBLE"
EVIDENCE_INSUFFICIENT: Final[str] = "EVIDENCE_INSUFFICIENT"
EVIDENCE_CONFLICT: Final[str] = "EVIDENCE_CONFLICT"
IDENTITY_CONFLICT: Final[str] = "IDENTITY_CONFLICT"
INDEX_BUSY: Final[str] = "INDEX_BUSY"
PREVIEW_EXPIRED: Final[str] = "PREVIEW_EXPIRED"
CONFIRMATION_REQUIRED: Final[str] = "CONFIRMATION_REQUIRED"
OPERATOR_REQUIRED: Final[str] = "OPERATOR_REQUIRED"
ADOPTION_WRITE_FAILED: Final[str] = "ADOPTION_WRITE_FAILED"

PREVIEW_TTL: Final[timedelta] = timedelta(minutes=10)
EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {"deployment_record", "ingestion_record", "benchmark_record"}
)
_FORBIDDEN_IDENTITY_KEYS: Final[frozenset[str]] = frozenset(
    {"api_key", "apikey", "secret", "token", "password", "authorization"}
)
_REQUIRED_EVIDENCE_FIELDS: Final[tuple[str, ...]] = (
    "provider",
    "adapter",
    "model_id",
    "dimension",
    "semantic_parameters",
)


class IndexIdentityError(RuntimeError):
    """索引身份操作的安全失败；只暴露稳定错误码与阶段。"""

    def __init__(self, code: str, *, stage: str = "identity") -> None:
        super().__init__(f"{code}：索引身份操作未完成（{stage}）。")
        self.code = code
        self.safe_error_code = code
        self.error_code = code
        self.stage = stage


@dataclass(frozen=True, slots=True)
class _Preview:
    preview_id: str
    kb_id: UUID
    environment_id: str
    management_session_id: str
    expected_identity_revision: int | None
    accepted_identity: dict[str, Any]
    evidence: list[dict[str, Any]]
    verified_facts: dict[str, Any]
    limitations: list[str]
    operator: dict[str, Any]
    operator_attestation: dict[str, Any]
    chunk_count: int
    vector_fingerprint: Any
    expires_at: datetime


def _normalize_json(value: Any) -> Any:
    """只保留可稳定比较的 JSON 值；不解释业务字段。"""

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if not key:
                raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage="json")
            normalized[key] = _normalize_json(raw_value)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime, UUID)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage="json")


def _text(value: Any, *, field: str, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage=f"field:{field}")
    if not isinstance(value, str):
        raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage=f"field:{field}")
    normalized = value.strip()
    if not normalized:
        if optional:
            return None
        raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage=f"field:{field}")
    return normalized


def _revision(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IndexIdentityError(IDENTITY_CONFLICT, stage="identity_revision")
    return value


def _dimension(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage="dimension")
    return value


def _assert_no_secret_keys(value: Mapping[str, Any]) -> None:
    for key in value:
        if str(key).strip().lower() in _FORBIDDEN_IDENTITY_KEYS:
            raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage="secret_field")


def normalize_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    """规范化 data-model §7 身份字段；未知值保持 ``None``，不补造。"""

    if not isinstance(identity, Mapping):
        raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage="identity")
    _assert_no_secret_keys(identity)
    provider = _text(identity.get("provider"), field="provider")
    adapter = _text(identity.get("adapter"), field="adapter")
    endpoint = _text(
        identity.get("endpoint_identity"), field="endpoint_identity", optional=True
    )
    model_id = _text(identity.get("model_id"), field="model_id")
    if model_id is not None and model_id.lower() == "unknown":
        raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage="model_id")
    model_version = _text(
        identity.get("model_version"), field="model_version", optional=True
    )
    dimension = _dimension(identity.get("dimension"))
    semantic_raw = identity.get("semantic_parameters")
    if semantic_raw is None:
        semantic_parameters: dict[str, Any] = {}
    elif isinstance(semantic_raw, Mapping):
        _assert_no_secret_keys(semantic_raw)
        semantic_parameters = copy.deepcopy(_normalize_json(semantic_raw))
    else:
        raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage="semantic_parameters")
    normalized = {
        "provider": provider,
        "adapter": adapter,
        "endpoint_identity": endpoint,
        "model_id": model_id,
        "model_version": model_version,
        "dimension": dimension,
        "semantic_parameters": semantic_parameters,
        "identity_revision": _revision(identity.get("identity_revision")) or 1,
    }
    status = identity.get("status")
    if status is not None:
        status_text = _text(status, field="status")
        if status_text not in IDENTITY_STATUSES:
            raise IndexIdentityError(INDEX_IDENTITY_INVALID, stage="status")
        normalized["status"] = status_text
    return normalized


def _identity_view(kb_id: UUID, facts: Mapping[str, Any]) -> dict[str, Any]:
    """把端口事实映射为只读 DTO；NULL 不回写。"""

    stored = facts.get("identity")
    stored_dimension = facts.get("stored_dimension")
    if stored is None:
        return {
            "kb_id": str(kb_id),
            "status": LEGACY_UNKNOWN,
            "dimension": (
                stored_dimension
                if stored_dimension is not None
                else EMBEDDING_VECTOR_DIMENSION
            ),
            "stored_dimension": stored_dimension,
            "model_id": None,
            "model_version": None,
            "provider": None,
            "adapter": None,
            "identity_revision": facts.get("identity_revision"),
            "adoption": None,
            "audits": [],
            "verified": False,
        }
    identity = copy.deepcopy(dict(stored))
    adoption = facts.get("adoption")
    if adoption is None:
        adoption = identity.get("adoption")
    audits = facts.get("audits")
    if audits is None:
        audits = identity.get("audits") or []
    view = {
        **identity,
        "kb_id": str(kb_id),
        "status": identity.get("status", NATIVE),
        "dimension": identity.get("dimension", stored_dimension),
        "stored_dimension": stored_dimension,
        "identity_revision": (
            facts.get("identity_revision")
            if facts.get("identity_revision") is not None
            else identity.get("identity_revision")
        ),
        "adoption": copy.deepcopy(adoption) if adoption is not None else None,
        "audits": copy.deepcopy(list(audits)),
        "verified": identity.get("status") in {NATIVE, LEGACY_ADOPTED},
    }
    return view


def _compatible(stored: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """判断两个已规范化身份是否属于同一个向量空间。"""

    if stored.get("status") == LEGACY_UNKNOWN:
        return False
    if stored.get("dimension") != candidate.get("dimension"):
        return False
    if stored.get("dimension") != EMBEDDING_VECTOR_DIMENSION:
        return False
    for field in ("provider", "adapter", "model_id"):
        if stored.get(field) != candidate.get(field):
            return False
    stored_endpoint = stored.get("endpoint_identity")
    candidate_endpoint = candidate.get("endpoint_identity")
    if stored_endpoint and candidate_endpoint and stored_endpoint != candidate_endpoint:
        return False
    stored_version = stored.get("model_version")
    candidate_version = candidate.get("model_version")
    if (
        stored_version is not None
        and candidate_version is not None
        and stored_version != candidate_version
    ):
        return False
    return stored.get("semantic_parameters") == candidate.get("semantic_parameters")


def _principal_facts(principal: Any) -> dict[str, Any]:
    if principal is None:
        raise IndexIdentityError(OPERATOR_REQUIRED, stage="operator")
    current = getattr(principal, "current", None)
    raw = current() if callable(current) else principal() if callable(principal) else principal
    if not isinstance(raw, Mapping):
        raise IndexIdentityError(OPERATOR_REQUIRED, stage="operator")
    principal_kind = _text(raw.get("principal_kind"), field="principal_kind")
    if principal_kind not in {"local_os", "container_operator"}:
        raise IndexIdentityError(OPERATOR_REQUIRED, stage="operator")
    return {
        "principal_kind": principal_kind,
        "principal_id": _text(raw.get("principal_id"), field="principal_id"),
        "environment_id": _text(raw.get("environment_id"), field="environment_id"),
        "instance_id": _text(raw.get("instance_id"), field="instance_id"),
        "management_session_id": _text(
            raw.get("management_session_id"), field="management_session_id"
        ),
    }


def _normalized_evidence(
    evidence: Sequence[Mapping[str, Any]], *, kb_id: UUID
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """校验证据结构、KB 范围与字段一致性；返回证据和字段事实。"""

    if not evidence:
        raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage="evidence")
    normalized: list[dict[str, Any]] = []
    seen: dict[str, Any] = {}
    for raw in evidence:
        if not isinstance(raw, Mapping):
            raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage="evidence")
        kind = _text(raw.get("kind"), field="kind")
        if kind not in EVIDENCE_KINDS:
            raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage="evidence_kind")
        source_reference = _text(raw.get("source_reference"), field="source_reference")
        source_date = _text(raw.get("source_date"), field="source_date")
        try:
            date.fromisoformat(str(source_date))
        except ValueError:
            raise IndexIdentityError(
                EVIDENCE_INSUFFICIENT, stage="source_date"
            ) from None
        scope = _text(raw.get("kb_scope"), field="kb_scope")
        if scope != str(kb_id):
            raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage="kb_scope")
        excerpt = _text(raw.get("safe_excerpt"), field="safe_excerpt")
        supported = raw.get("supported_fields")
        if not isinstance(supported, Mapping) or not supported:
            raise IndexIdentityError(
                EVIDENCE_INSUFFICIENT, stage="supported_fields"
            )
        fields = copy.deepcopy(_normalize_json(supported))
        for field, value in fields.items():
            if field in seen and seen[field] != value:
                raise IndexIdentityError(EVIDENCE_CONFLICT, stage="evidence")
            seen[field] = value
        normalized.append(
            {
                "kind": kind,
                "source_reference": source_reference,
                "source_date": source_date,
                "kb_scope": scope,
                "safe_excerpt": excerpt,
                "supported_fields": fields,
            }
        )
    missing = [field for field in _REQUIRED_EVIDENCE_FIELDS if field not in seen]
    if missing:
        raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage="evidence_fields")
    return normalized, seen


class AdoptionPreviewStore:
    """Runtime-owned previews; DB sessions and repositories remain request scoped."""

    def __init__(self) -> None:
        self.previews: dict[str, _Preview] = {}
        self.lock = threading.Lock()


class IndexIdentityService:
    """索引身份读取、首次登记、兼容检查与历史接纳领域服务。"""

    def __init__(
        self,
        *,
        repository: Any,
        clock: Callable[[], datetime] | None = None,
        principal: Any = None,
        preview_store: AdoptionPreviewStore | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._principal = principal
        store = preview_store or AdoptionPreviewStore()
        self._previews = store.previews
        self._lock = store.lock

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _read(self, kb_id: UUID) -> dict[str, Any]:
        try:
            facts = self._repository.read(kb_id)
        except IndexIdentityError:
            raise
        except Exception as exc:  # 端口异常不泄露数据库正文。
            raise IndexIdentityError(
                INDEX_IDENTITY_NOT_FOUND, stage="read"
            ) from exc
        if not isinstance(facts, Mapping):
            raise IndexIdentityError(INDEX_IDENTITY_NOT_FOUND, stage="read")
        return dict(facts)

    def view(self, *, kb_id: UUID) -> dict[str, Any]:
        """只读身份视图；历史 NULL 映射 ``legacy_unknown``，不写回。"""

        return _identity_view(kb_id, self._read(kb_id))

    @contextmanager
    def begin(
        self, kb_id: UUID, *, timeout: float = 5.0
    ) -> Iterator[Any]:
        """打开调用方拥有的 KB 行锁事务；提交/回滚仍由调用方决定。"""

        with self._repository.begin(kb_id, timeout=timeout) as unit_of_work:
            yield unit_of_work

    def require_compatible(
        self, *, kb_id: UUID, identity: Mapping[str, Any]
    ) -> dict[str, Any]:
        """在查询或新增摄取前拒绝跨向量空间使用。"""

        candidate = normalize_identity(identity)
        if candidate["dimension"] != EMBEDDING_VECTOR_DIMENSION:
            raise IndexIdentityError(EMBEDDING_INCOMPATIBLE, stage="dimension")
        facts = self._read(kb_id)
        stored = facts.get("identity")
        if stored is None:
            if int(facts.get("chunk_count") or 0) > 0:
                raise IndexIdentityError(
                    EMBEDDING_INCOMPATIBLE, stage="legacy_unknown"
                )
            return _identity_view(kb_id, facts)
        normalized_stored = normalize_identity(stored)
        normalized_stored["status"] = stored.get("status", NATIVE)
        stored_dimension = facts.get("stored_dimension")
        if stored_dimension is not None and stored_dimension != candidate["dimension"]:
            raise IndexIdentityError(EMBEDDING_INCOMPATIBLE, stage="stored_dimension")
        if not _compatible(normalized_stored, candidate):
            raise IndexIdentityError(EMBEDDING_INCOMPATIBLE, stage="identity")
        return _identity_view(kb_id, facts)

    def register_native(
        self,
        *,
        kb_id: UUID,
        identity: Mapping[str, Any],
        unit_of_work: Any,
    ) -> dict[str, Any]:
        """在调用方事务内登记首次 native 身份；不自行提交。"""

        candidate = normalize_identity(identity)
        if candidate["dimension"] != EMBEDDING_VECTOR_DIMENSION:
            raise IndexIdentityError(EMBEDDING_INCOMPATIBLE, stage="dimension")
        facts = dict(unit_of_work.read())
        if facts.get("identity") is not None:
            raise IndexIdentityError(IDENTITY_CONFLICT, stage="native")
        if int(facts.get("chunk_count") or 0) > 0:
            raise IndexIdentityError(
                EMBEDDING_INCOMPATIBLE, stage="legacy_unknown"
            )
        stored_dimension = facts.get("stored_dimension")
        if stored_dimension is not None and stored_dimension != candidate["dimension"]:
            raise IndexIdentityError(EMBEDDING_INCOMPATIBLE, stage="stored_dimension")
        native = {
            **candidate,
            "status": NATIVE,
            "identity_revision": candidate.get("identity_revision") or 1,
            "registered_at": self._now().isoformat(),
        }
        unit_of_work.write_identity(native)
        return _identity_view(kb_id, {**facts, "identity": native})

    def preview_adoption(
        self,
        *,
        kb_id: UUID,
        expected_identity_revision: int | None,
        proposed_identity: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
        operator_attestation: Mapping[str, Any],
        management_session_id: str,
    ) -> dict[str, Any]:
        """生成 10 分钟、仅同会话可确认的只读接纳预览。"""

        operator = _principal_facts(self._principal)
        if management_session_id != operator["management_session_id"]:
            raise IndexIdentityError(OPERATOR_REQUIRED, stage="session")
        if not isinstance(operator_attestation, Mapping):
            raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage="attestation")
        if operator_attestation.get("verified") is not True:
            raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage="attestation")
        covers = _text(
            operator_attestation.get("covers"), field="attestation_covers"
        )
        expected = _revision(expected_identity_revision)
        candidate = normalize_identity(proposed_identity)
        if candidate["dimension"] != EMBEDDING_VECTOR_DIMENSION:
            raise IndexIdentityError(EMBEDDING_INCOMPATIBLE, stage="dimension")
        facts = self._read(kb_id)
        if facts.get("identity") is not None:
            raise IndexIdentityError(IDENTITY_CONFLICT, stage="preview")
        current_revision = _revision(facts.get("identity_revision"))
        if current_revision != expected:
            raise IndexIdentityError(IDENTITY_CONFLICT, stage="identity_revision")
        stored_dimension = facts.get("stored_dimension")
        if stored_dimension is None:
            raise IndexIdentityError(EVIDENCE_INSUFFICIENT, stage="stored_dimension")
        if stored_dimension != candidate["dimension"]:
            raise IndexIdentityError(EMBEDDING_INCOMPATIBLE, stage="stored_dimension")
        if int(facts.get("chunk_count") or 0) <= 0:
            raise IndexIdentityError(IDENTITY_CONFLICT, stage="no_legacy_vectors")
        normalized_evidence, supported = _normalized_evidence(evidence, kb_id=kb_id)
        for field in _REQUIRED_EVIDENCE_FIELDS:
            value = supported.get(field)
            proposed_value = candidate.get(field)
            if isinstance(proposed_value, Mapping):
                proposed_value = _normalize_json(proposed_value)
            if value != proposed_value:
                raise IndexIdentityError(EVIDENCE_CONFLICT, stage=f"field:{field}")
        accepted = {
            **candidate,
            "status": LEGACY_ADOPTED,
            "identity_revision": (expected or 0) + 1,
        }
        evidence_version = supported.get("model_version")
        if accepted.get("model_version") is None and isinstance(evidence_version, str):
            accepted["model_version"] = evidence_version.strip() or None
        limitations: list[str] = []
        if accepted.get("model_version") is None:
            limitations.append("model_version_unknown")
        if "endpoint_identity" not in supported:
            limitations.append("endpoint_identity_not_attested")
        preview_id = str(uuid4())
        now = self._now()
        record = _Preview(
            preview_id=preview_id,
            kb_id=kb_id,
            environment_id=str(operator["environment_id"]),
            management_session_id=str(operator["management_session_id"]),
            expected_identity_revision=expected,
            accepted_identity=accepted,
            evidence=normalized_evidence,
            verified_facts={
                "stored_dimension": stored_dimension,
                "schema_head": None,
                "evidence_field_consistency": True,
                "chunk_count": int(facts.get("chunk_count") or 0),
            },
            limitations=limitations,
            operator=operator,
            operator_attestation={"verified": True, "covers": covers},
            chunk_count=int(facts.get("chunk_count") or 0),
            vector_fingerprint=copy.deepcopy(facts.get("vector_fingerprint")),
            expires_at=now + PREVIEW_TTL,
        )
        with self._lock:
            for key, previous in tuple(self._previews.items()):
                if previous.expires_at < now:
                    del self._previews[key]
            self._previews[preview_id] = record
        return {
            "preview_id": preview_id,
            "environment_id": record.environment_id,
            "kb_id": str(kb_id),
            "expected_identity_revision": expected,
            "previous_status": LEGACY_UNKNOWN,
            "previous_identity": None,
            "proposed_identity": copy.deepcopy(accepted),
            "accepted_identity": copy.deepcopy(accepted),
            "verified_facts": copy.deepcopy(record.verified_facts),
            "evidence": copy.deepcopy(normalized_evidence),
            "limitations": list(limitations),
            "operator_attestation": copy.deepcopy(record.operator_attestation),
            "expires_at": record.expires_at.isoformat(),
            "requires_confirmation": True,
            "modifies_vectors": False,
        }

    def _preview(self, preview_id: Any) -> _Preview:
        with self._lock:
            record = self._previews.get(str(preview_id))
        if record is None:
            raise IndexIdentityError(PREVIEW_EXPIRED, stage="preview")
        if self._now() > record.expires_at:
            raise IndexIdentityError(PREVIEW_EXPIRED, stage="preview")
        return record

    def _confirm_idempotent(
        self,
        kb_id: UUID,
        facts: Mapping[str, Any],
        confirmation_id: UUID,
    ) -> dict[str, Any] | None:
        adoption = facts.get("adoption")
        if adoption is None and isinstance(facts.get("identity"), Mapping):
            adoption = facts["identity"].get("adoption")
        if isinstance(adoption, Mapping) and str(adoption.get("confirmation_id")) == str(
            confirmation_id
        ):
            # FakeUnitOfWork.read 是锁内最小事实，不包含完整审计列表；
            # 幂等读回走只读端口，返回已保存的完整接纳事实。
            return _identity_view(kb_id, self._read(kb_id))
        return None

    def confirm_adoption(
        self,
        *,
        preview_id: Any,
        expected_identity_revision: int | None,
        confirmation_id: UUID,
        management_session_id: str,
        confirm: bool,
        expected_kb_id: UUID | None = None,
    ) -> dict[str, Any]:
        """在 KB 行锁内原子保存 legacy_adopted、接纳事实与审计。"""

        if confirm is not True:
            raise IndexIdentityError(CONFIRMATION_REQUIRED, stage="confirm")
        record = self._preview(preview_id)
        if expected_kb_id is not None and record.kb_id != expected_kb_id:
            raise IndexIdentityError(IDENTITY_CONFLICT, stage="kb_scope")
        if management_session_id != record.management_session_id:
            raise IndexIdentityError(OPERATOR_REQUIRED, stage="session")
        if _revision(expected_identity_revision) != record.expected_identity_revision:
            raise IndexIdentityError(IDENTITY_CONFLICT, stage="identity_revision")
        try:
            with self._repository.begin(record.kb_id) as unit_of_work:
                facts = dict(unit_of_work.read())
                if facts.get("identity") is not None:
                    existing = self._confirm_idempotent(
                        record.kb_id, facts, confirmation_id
                    )
                    if existing is not None:
                        return existing
                    raise IndexIdentityError(IDENTITY_CONFLICT, stage="already_known")
                if _revision(facts.get("identity_revision")) != record.expected_identity_revision:
                    raise IndexIdentityError(
                        IDENTITY_CONFLICT, stage="identity_revision"
                    )
                if (
                    int(facts.get("chunk_count") or 0) != record.chunk_count
                    or facts.get("vector_fingerprint") != record.vector_fingerprint
                ):
                    raise IndexIdentityError(INDEX_BUSY, stage="new_ingestion")
                if facts.get("stored_dimension") != record.accepted_identity["dimension"]:
                    raise IndexIdentityError(
                        EMBEDDING_INCOMPATIBLE, stage="stored_dimension"
                    )
                now = self._now()
                adoption_id = str(uuid4())
                adoption = {
                    "adoption_id": adoption_id,
                    "confirmation_id": str(confirmation_id),
                    "accepted_at": now.isoformat(),
                    "operator": copy.deepcopy(record.operator),
                    "previous_status": LEGACY_UNKNOWN,
                    "previous_identity": None,
                    "accepted_identity": copy.deepcopy(record.accepted_identity),
                    "evidence": copy.deepcopy(record.evidence),
                    "verified_facts": copy.deepcopy(record.verified_facts),
                    "operator_attestation": copy.deepcopy(
                        record.operator_attestation
                    ),
                    "limitations": list(record.limitations),
                }
                audit = {
                    "audit_id": str(uuid4()),
                    "action": "embedding_identity.adopted",
                    "adoption_id": adoption_id,
                    "confirmation_id": str(confirmation_id),
                    "kb_id": str(record.kb_id),
                    "accepted_at": now.isoformat(),
                    "operator": copy.deepcopy(record.operator),
                    "previous_status": LEGACY_UNKNOWN,
                    "previous_identity": None,
                    "identity_revision": record.accepted_identity["identity_revision"],
                    "evidence_count": len(record.evidence),
                    "limitations": list(record.limitations),
                }
                unit_of_work.write_identity(copy.deepcopy(record.accepted_identity))
                unit_of_work.write_adoption(adoption)
                unit_of_work.write_audit(audit)
                unit_of_work.commit()
        except IndexIdentityError:
            raise
        except Exception as exc:  # 端口写入失败只暴露安全阶段码。
            raise IndexIdentityError(ADOPTION_WRITE_FAILED, stage="commit") from exc
        facts = self._read(record.kb_id)
        return _identity_view(record.kb_id, facts)


class SqlAlchemyIndexUnitOfWork:
    """同一 Session 上的 KB 行锁事务；身份、接纳和审计写入同一 JSONB。"""

    def __init__(self, repository: SqlAlchemyIndexRepository, kb: KnowledgeBase) -> None:
        self._repository = repository
        self._session = repository.session
        self._kb = kb

    def read(self) -> dict[str, Any]:
        return self._repository.facts_for(self._kb)

    def write_identity(
        self, identity: Mapping[str, Any] | None = None, **extra: Any
    ) -> None:
        if identity is not None:
            self._kb.embedding_identity = copy.deepcopy(dict(identity))
            for name, value in extra.items():
                self._kb.embedding_identity[name] = copy.deepcopy(value)
        elif extra:
            current = copy.deepcopy(self._kb.embedding_identity or {})
            for name, value in extra.items():
                current[name] = copy.deepcopy(value)
            self._kb.embedding_identity = current
        flag_modified(self._kb, "embedding_identity")

    def _merge(self, name: str, value: Any) -> None:
        current = copy.deepcopy(self._kb.embedding_identity or {})
        current[name] = copy.deepcopy(value)
        self._kb.embedding_identity = current
        flag_modified(self._kb, "embedding_identity")

    def write_adoption(self, adoption: Mapping[str, Any], **extra: Any) -> None:
        current = copy.deepcopy(self._kb.embedding_identity or {})
        current["adoption"] = copy.deepcopy(dict(adoption))
        for name, value in extra.items():
            current[name] = copy.deepcopy(value)
        self._kb.embedding_identity = current
        flag_modified(self._kb, "embedding_identity")

    def write_audit(self, audit: Mapping[str, Any]) -> None:
        current = copy.deepcopy(self._kb.embedding_identity or {})
        audits = list(current.get("audits") or [])
        audits.append(copy.deepcopy(dict(audit)))
        current["audits"] = audits
        self._kb.embedding_identity = current
        flag_modified(self._kb, "embedding_identity")

    def commit(self) -> None:
        try:
            self._session.commit()
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise IndexIdentityError(ADOPTION_WRITE_FAILED, stage="commit") from exc

    def rollback(self) -> None:
        self._session.rollback()


class SqlAlchemyIndexRepository:
    """KnowledgeBase 身份事实的 SQLAlchemy 端口，包含行锁和实际维度查询。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _id(kb_id: UUID | str) -> UUID:
        try:
            return kb_id if isinstance(kb_id, UUID) else UUID(str(kb_id))
        except (TypeError, ValueError):
            raise IndexIdentityError(INDEX_IDENTITY_NOT_FOUND, stage="kb_id") from None

    def _load(self, kb_id: UUID | str) -> KnowledgeBase:
        kb = self.session.get(KnowledgeBase, self._id(kb_id))
        if kb is None:
            raise IndexIdentityError(INDEX_IDENTITY_NOT_FOUND, stage="read")
        return kb

    def facts_for(self, kb: KnowledgeBase) -> dict[str, Any]:
        identity = copy.deepcopy(kb.embedding_identity)
        adoption = identity.get("adoption") if isinstance(identity, Mapping) else None
        audits = identity.get("audits") if isinstance(identity, Mapping) else None
        chunk_count, latest = self.session.execute(
            select(
                func.count(DocumentChunk.id),
                func.max(DocumentChunk.updated_at),
            ).where(DocumentChunk.knowledge_base_id == kb.id)
        ).one()
        embedding = self.session.scalar(
            select(DocumentChunk.embedding)
            .where(
                DocumentChunk.knowledge_base_id == kb.id,
                DocumentChunk.embedding.is_not(None),
            )
            .limit(1)
        )
        stored_dimension = len(embedding) if embedding is not None else None
        return {
            "kb_id": kb.id,
            "identity": identity,
            "identity_revision": (
                identity.get("identity_revision")
                if isinstance(identity, Mapping)
                else None
            ),
            "adoption": copy.deepcopy(adoption) if adoption is not None else None,
            "audits": copy.deepcopy(list(audits or [])),
            "commit_count": 0,
            "writes_without_audit": 0,
            "vector_fingerprint": (
                int(chunk_count),
                latest.isoformat() if latest is not None else None,
            ),
            "stored_dimension": stored_dimension,
            "chunk_count": int(chunk_count),
        }

    def read(self, kb_id: UUID | str) -> dict[str, Any]:
        return self.facts_for(self._load(kb_id))

    @contextmanager
    def begin(
        self, kb_id: UUID | str, *, timeout: float = 5.0
    ) -> Iterator[SqlAlchemyIndexUnitOfWork]:
        normalized = self._id(kb_id)
        bind = self.session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            milliseconds = max(1, int(timeout * 1000))
            try:
                self.session.execute(
                    text(f"SET LOCAL lock_timeout = '{milliseconds}ms'")
                )
            except SQLAlchemyError as exc:
                self.session.rollback()
                raise IndexIdentityError(INDEX_BUSY, stage="lock_timeout") from exc
        try:
            kb = self.session.scalar(
                select(KnowledgeBase)
                .where(KnowledgeBase.id == normalized)
                .with_for_update()
            )
        except OperationalError as exc:
            self.session.rollback()
            if "lock timeout" in str(exc).lower() or "canceling statement" in str(
                exc
            ).lower():
                raise IndexIdentityError(INDEX_BUSY, stage="lock") from None
            raise IndexIdentityError(INDEX_BUSY, stage="lock") from exc
        if kb is None:
            self.session.rollback()
            raise IndexIdentityError(INDEX_IDENTITY_NOT_FOUND, stage="lock")
        try:
            yield SqlAlchemyIndexUnitOfWork(self, kb)
        except Exception:
            self.session.rollback()
            raise


__all__ = [
    "ADOPTION_WRITE_FAILED",
    "CONFIRMATION_REQUIRED",
    "EMBEDDING_INCOMPATIBLE",
    "EVIDENCE_CONFLICT",
    "EVIDENCE_INSUFFICIENT",
    "IDENTITY_CONFLICT",
    "INDEX_BUSY",
    "INDEX_IDENTITY_INVALID",
    "INDEX_IDENTITY_NOT_FOUND",
    "LEGACY_ADOPTED",
    "LEGACY_UNKNOWN",
    "NATIVE",
    "OPERATOR_REQUIRED",
    "PREVIEW_EXPIRED",
    "IndexIdentityError",
    "IndexIdentityService",
    "SqlAlchemyIndexRepository",
    "SqlAlchemyIndexUnitOfWork",
    "normalize_identity",
]
