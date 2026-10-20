"""TCR-S5-RUNTIME-SNAPSHOT-INDEX-013：T211 观察端口与替身，不实现产品决策。

T212（不可变运行快照／旧版工作流状态）与 T214（Embedding 索引语义身份）在本轮尚未
实现：缺失入口必须在 call 阶段以 ``T212_IMPLEMENTATION_REQUIRED`` 或
``T214_IMPLEMENTATION_REQUIRED`` 显式失败（0 收集错误、不是 xfail），而不是破坏收集或
把缺失当通过。

下面的适配器只是测试接缝：实现方可以改名，但不得削弱三个 T211 测试文件里的行为断言。
替身只提供合成输入、端口级行锁、故障注入、可控时钟与服务端操作者事实，不做快照、
身份、接纳或兼容性判断——这些决定必须由产品代码做出。
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from backend.app.ai.workflows.state import (
    CHECKPOINT_STATE_KEY,
    WORKFLOW_STATE_PAYLOAD_KIND,
    WORKFLOW_STATE_PAYLOAD_VERSION,
    workflow_state_to_json,
)
from backend.app.domain.enums import (
    QuestionType,
    ReviewStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.schemas.grading import (
    ExamResultDTO,
    ExamResultStatus,
    QuestionResultDTO,
    SubmissionContext,
)
from backend.app.services.settings.config_service import ConfigService
from tests.support.credential_fakes import (
    FakeFileSecurity,
    FakeProtector,
    assert_no_secret,
    baseline_values,
    state_of,
)

#: 缺失实现的分类标记（call 阶段显式失败，不污染收集阶段）。
T212 = "T212"
T214 = "T214"
#: 任务与 plan §2 定义的模块路径。
SNAPSHOT_MODULE = "backend.app.services.settings.runtime_snapshot"
INDEX_MODULE = "backend.app.services.settings.index_identity"

#: data-model §6 / contracts/runtime-configuration §4 的安全错误码。
SNAPSHOT_UNAVAILABLE = "SNAPSHOT_UNAVAILABLE"
SNAPSHOT_DEPENDENCY_UNAVAILABLE = "SNAPSHOT_DEPENDENCY_UNAVAILABLE"
#: data-model §6/§7 的状态取值。
NATIVE = "native"
LEGACY_UNKNOWN = "legacy_unknown"
LEGACY_ADOPTED = "legacy_adopted"
#: contracts/settings-api §6 的错误码。
EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
EMBEDDING_INCOMPATIBLE = "EMBEDDING_INCOMPATIBLE"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
INDEX_BUSY = "INDEX_BUSY"
PREVIEW_EXPIRED = "PREVIEW_EXPIRED"
#: settings-api §6 把“身份变化/摄取冲突”归入这一对 409。
CONFLICT_CODES = frozenset({IDENTITY_CONFLICT, INDEX_BUSY})


# ---------------------------------------------------------------------------
# 通用接缝工具
# ---------------------------------------------------------------------------


def required_symbol(module_name: str, symbol: str, marker: str) -> Any:
    """缺失模块/符号保持“入口未实现”，与真实 ImportError 区分。"""

    if importlib.util.find_spec(module_name) is None:
        pytest.fail(f"{marker}_IMPLEMENTATION_REQUIRED: {module_name}.{symbol}", pytrace=False)
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol):
        pytest.fail(f"{marker}_IMPLEMENTATION_REQUIRED: {module_name}.{symbol}", pytrace=False)
    return getattr(module, symbol)


def required_method(owner: Any, name: str, marker: str) -> Any:
    method = getattr(owner, name, None)
    if not callable(method):
        pytest.fail(
            f"{marker}_IMPLEMENTATION_REQUIRED: {type(owner).__module__}.{type(owner).__name__}.{name}",
            pytrace=False,
        )
    return method


def method(owner: Any, name: str) -> Any:
    """已实现依赖（T201/T210）的入口；缺失就是真实缺陷而不是“未实现”。"""

    found = getattr(owner, name, None)
    assert callable(found), f"existing entry point is missing: {type(owner).__name__}.{name}"
    return found


def accepts_keyword(func: Any, name: str) -> bool:
    """判定调用是否接受某关键字参数；无法内省时交给真实调用决定。"""

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - 内建/扩展调用
        return True
    return name in signature.parameters or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()
    )


def facts_of(value: Any) -> Any:
    """只做序列化适配：不解释、不补默认值。

    最终状态等观察值可以是标量；观察适配不得把合法字符串误判成测试设施错误。
    """

    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else dumped
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"T211 test port expected a mapping, Pydantic DTO or scalar, got {type(value).__name__}"
    )


def facts_text(value: Any) -> str:
    """事实的稳定文本表示，用于跨实例/跨进程比较。"""

    return json.dumps(facts_of(value), ensure_ascii=False, sort_keys=True, default=str)


def safe_code(value: Any) -> str:
    """读取产品或异常上的安全错误码；不读正文、不回显秘密。"""

    for name in ("safe_error_code", "error_code", "code", "result_code", "reason"):
        candidate = getattr(value, name, None)
        if isinstance(candidate, str) and candidate:
            return candidate
    if hasattr(value, "model_dump") or isinstance(value, Mapping):
        data = facts_of(value)
        for name in ("safe_error_code", "error_code", "code", "result_code", "reason"):
            candidate = data.get(name)
            if isinstance(candidate, str) and candidate:
                return candidate
    raise AssertionError("product result carries no safe error code")


def value_of(facts: Mapping[str, Any], name: str) -> Any:
    """取目标字段；缺失即“实现未暴露该事实”。"""

    if name not in facts:
        pytest.fail(f"T211_IMPLEMENTATION_REQUIRED: missing observable field {name!r}", pytrace=False)
    return facts[name]


def credential_state(store: Any, reference: Any, version: UUID | None = None) -> str:
    status = store.status(reference, version or reference.version)
    return str(state_of(status))


# ---------------------------------------------------------------------------
# 合成配置 + 受控接纳设施（复用 T201/T205/T206/T209/T210 已实现入口）
# ---------------------------------------------------------------------------


class FakeClock:
    """可控 UTC 时钟；只回答“现在几点”，不决定任何过期策略。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 17, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: Any) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


@dataclass
class Admission:
    """真实 Registry/ProbeService 之上的观察端口；MockTransport 代替外网。"""

    registry: Any
    probes: Any
    requests: list[Any] = field(default_factory=list)

    async def evidence(
        self,
        draft: Any,
        capabilities: tuple[str, ...] = ("text", "json_object"),
    ) -> list[Any]:
        profile = next(iter(draft.values.providers.values()))
        results = []
        for capability in capabilities:
            results.append(
                await self.probes.run(
                    candidate_revision=draft.revision_id,
                    profile=profile,
                    capability=capability,
                    allow_billable=True,
                )
            )
        return results


def make_admission(service: ConfigService, store: Any, clock: Any) -> Admission:
    """真实 ProviderRegistry + ProbeService；HTTP 由 MockTransport 承载。"""

    from backend.app.services.settings.probe_service import ProbeService
    from backend.app.services.settings.provider_registry import ProviderRegistry

    registry = ProviderRegistry(environment_id=service.environment_id)
    requests: list[Any] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "t211-synthetic",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok": true}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    probes = ProbeService(
        environment_id=service.environment_id,
        registry=registry,
        credential_store=store,
        clock=clock,
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    return Admission(registry=registry, probes=probes, requests=requests)


def ai_values(
    root: Path,
    environment_id: UUID,
    *,
    jwt_reference: Any,
    model_id: str,
    confidence_threshold: float = 0.8,
    base_url: str = "https://model.invalid/v1",
    auth_mode: str = "none",
) -> dict[str, Any]:
    """一份无秘密的合成配置值：默认 LLM 绑定 + 策略，凭证可选。"""

    values = baseline_values(root, environment_id, jwt_reference)
    profile_id, revision = uuid4(), uuid4()
    values["providers"] = {
        str(profile_id): {
            "profile_id": str(profile_id),
            "profile_revision": str(revision),
            "display_name": "T211 synthetic model",
            "adapter_id": "openai_compatible",
            "model_id": model_id,
            "base_url": base_url,
            "auth_mode": auth_mode,
            "declared_capabilities": ["text", "json_object"],
            "enabled": True,
        }
    }
    values["bindings"] = {
        "default_llm": {
            "task_kind": "default_llm",
            "profile_id": str(profile_id),
            "profile_revision": str(revision),
        }
    }
    values["policies"] = {"confidence_threshold": confidence_threshold}
    return values


@dataclass
class SettingsHarness:
    """隔离环境根上的真实 ConfigService + 自有凭据库 + 受控 Probe。"""

    root: Path
    environment_id: UUID
    service: ConfigService
    store: Any
    admission: Admission
    clock: FakeClock
    protector: FakeProtector
    jwt_reference: Any
    provider_reference: Any = None
    business_guard: Any = None

    def reopen(self) -> SettingsHarness:
        """同一环境根、同一 OS 用户保护器的“重启后”新实例。"""

        return settings_harness(
            self.root,
            environment_id=self.environment_id,
            protector=self.protector,
            clock=self.clock,
        )

    def close(self) -> None:
        self.service.close()

    def values(self, model_id: str, *, confidence_threshold: float = 0.8) -> dict[str, Any]:
        return ai_values(
            self.root,
            self.environment_id,
            jwt_reference=self.jwt_reference,
            model_id=model_id,
            confidence_threshold=confidence_threshold,
        )

    def draft(self, values: dict[str, Any], *, expected: UUID | None = None) -> Any:
        candidate = self.service.create_draft(values, expected_revision=expected, process_env={})
        self.service.validate(candidate.revision_id, expected_revision=expected, process_env={})
        return candidate

    async def activate(
        self,
        candidate: Any,
        *,
        expected: UUID | None = None,
        capabilities: tuple[str, ...] = ("text", "json_object"),
    ) -> Any:
        evidence = await self.admission.evidence(candidate, capabilities)
        return self.service.activate(
            candidate.revision_id,
            expected_revision=expected,
            probe_ids=[item.probe_id for item in evidence],
            process_env={},
        )

    async def activate_model(
        self,
        model_id: str,
        *,
        expected: UUID | None = None,
        confidence_threshold: float = 0.8,
        with_credential: bool = False,
    ) -> Any:
        values = self.values(model_id, confidence_threshold=confidence_threshold)
        if with_credential:
            reference = self.store.put(
                purpose="provider", secret_value=SecretStr("t211_synthetic_provider_" + uuid4().hex)
            )
            profile_key = next(iter(values["providers"]))
            values["providers"][profile_key]["credential_ref"] = reference.model_dump(mode="json")
            values["providers"][profile_key]["auth_mode"] = "api_key"
            self.provider_reference = reference
        return await self.activate(self.draft(values, expected=expected), expected=expected)


def settings_harness(
    root: Path,
    *,
    environment_id: UUID | None = None,
    protector: Any = None,
    clock: Any = None,
) -> SettingsHarness:
    """在隔离目录建立真实设置服务；不读写任何真实用户目录。"""

    root = Path(root).resolve()
    service = ConfigService(root, environment_id or uuid4())
    opener = method(service, "open_credentials")
    shared_protector = protector or FakeProtector()
    store = opener(backend="dpapi", protector=shared_protector, file_security=FakeFileSecurity())
    resolved_clock = clock or FakeClock()
    admission = make_admission(service, store, resolved_clock)
    jwt_reference = store.put(purpose="jwt", secret_value=SecretStr("t211_synthetic_jwt_" + uuid4().hex))
    wire_admission(service, admission, clock=resolved_clock)
    return SettingsHarness(
        root=root,
        environment_id=service.environment_id,
        service=service,
        store=store,
        admission=admission,
        clock=resolved_clock,
        protector=shared_protector,
        jwt_reference=jwt_reference,
        business_guard=lambda old, new: True,
    )


def wire_admission(
    service: ConfigService,
    admission: Admission,
    *,
    business_guard: Any = None,
    runtime_references: Any = None,
    legacy_runs: Any = None,
    clock: Any = None,
) -> None:
    """接入 T210 已实现的接纳入口；`legacy_runs` 是 T212 必须提供的事实源。"""

    configure = method(service, "configure_admission")
    kwargs: dict[str, Any] = {
        "registry": admission.registry,
        "probe_service": admission.probes,
        "business_guard": business_guard if business_guard is not None else (lambda old, new: True),
        "runtime_references": runtime_references,
        "clock": clock,
    }
    if legacy_runs is not None:
        if not accepts_keyword(configure, "legacy_runs"):
            pytest.fail(
                "T212_IMPLEMENTATION_REQUIRED: ConfigService.configure_admission(legacy_runs=...)",
                pytrace=False,
            )
        kwargs["legacy_runs"] = legacy_runs
    configure(**kwargs)


def legacy_run_fact(
    workflow_id: str,
    *,
    status: str = "Paused",
    task_kinds: tuple[str, ...] = ("subjective_grading",),
) -> dict[str, Any]:
    """一条缺快照的非终态运行事实（服务端只读输入）。"""

    return {
        "workflow_id": workflow_id,
        "status": status,
        "task_kinds": list(task_kinds),
        "snapshot_id": None,
    }


# ---------------------------------------------------------------------------
# 运行快照接缝（T212）
# ---------------------------------------------------------------------------


def snapshot_service(config_service: ConfigService) -> Any:
    factory = required_symbol(SNAPSHOT_MODULE, "RuntimeSnapshotService", T212)
    return factory(config_service)


def snapshot_facts(snapshot: Any) -> dict[str, Any]:
    facts = facts_of(snapshot)
    if not isinstance(facts.get("snapshot_id"), (str, UUID)):
        pytest.fail("T212_IMPLEMENTATION_REQUIRED: RuntimeSnapshot.snapshot_id", pytrace=False)
    return facts


def create_snapshot(
    service: Any,
    *,
    workflow_id: str,
    request_id: str = "t211-request",
    task_kinds: Sequence[str] = ("subjective_grading",),
) -> Any:
    creator = required_method(service, "create", T212)
    return snapshot_facts(
        creator(workflow_id=workflow_id, request_id=request_id, task_kinds=tuple(task_kinds))
    )


def resolve_snapshot(service: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    """解析运行状态的快照边界：native 快照或显式 legacy_unknown。"""

    resolver = required_method(service, "resolve_for_state", T212)
    result = facts_of(resolver(dict(state)))
    if "origin" not in result or "snapshot" not in result:
        pytest.fail(
            "T212_IMPLEMENTATION_REQUIRED: RuntimeSnapshotService.resolve_for_state(state) -> {snapshot, origin}",
            pytrace=False,
        )
    return result


def snapshot_root(root: Path) -> Path:
    return Path(root) / "config" / "snapshots"


def snapshot_files(root: Path) -> dict[str, bytes]:
    target = snapshot_root(root)
    if not target.exists():
        return {}
    return {path.relative_to(target).as_posix(): path.read_bytes() for path in sorted(target.rglob("*")) if path.is_file()}


# ---------------------------------------------------------------------------
# 旧/新版工作流状态样本与业务断言夹具
# ---------------------------------------------------------------------------


def sample_context() -> SubmissionContext:
    return SubmissionContext.model_validate(
        {
            "submission_id": "submission-t211",
            "exam_id": "exam-t211",
            "student_id": "student-t211",
            "expected_answers": [
                {
                    "order": 1,
                    "answer_id": "answer-t211-1",
                    "question_id": "question-t211-1",
                    "question_type": QuestionType.SHORT_ANSWER.value,
                    "max_score": "10.00",
                    "knowledge_points": ["函数极限"],
                },
                {
                    "order": 2,
                    "answer_id": "answer-t211-2",
                    "question_id": "question-t211-2",
                    "question_type": QuestionType.SINGLE_CHOICE.value,
                    "max_score": "5.00",
                    "knowledge_points": ["导数定义"],
                },
            ],
        }
    )


def business_items(*, pending_review: bool = False) -> tuple[list[QuestionResultDTO], ExamResultDTO]:
    """逐题结果与整卷结果：待复核题目不计入（原业务事实，非本轮新语义）。"""

    counted = QuestionResultDTO(
        order=1,
        answer_id="answer-t211-1",
        question_id="question-t211-1",
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal("10.00"),
        score=Decimal("8.50"),
        effective_score=Decimal("8.50"),
        counted=True,
        missing=False,
        requires_review=False,
        grading_status="Accepted",
        review_status=ReviewStatus.NOT_REQUIRED.value,
        validation_status=ValidationStatus.VALIDATED.value,
        reason="要点基本齐全。",
        knowledge_points=["函数极限"],
        missing_knowledge_points=["连续性"],
        correct_points=["左右极限相等"],
        suggestions=["复习连续性与极限的关系。"],
        retrieved_context_ids=["chunk-1"],
        confidence=0.9,
        submission_id="submission-t211",
        decision=None,
    )
    second = QuestionResultDTO(
        order=2,
        answer_id="answer-t211-2",
        question_id="question-t211-2",
        question_type=QuestionType.SINGLE_CHOICE,
        max_score=Decimal("5.00"),
        score=Decimal("2.50"),
        effective_score=None if pending_review else Decimal("2.50"),
        counted=not pending_review,
        missing=False,
        requires_review=pending_review,
        grading_status="Pending Review" if pending_review else "Accepted",
        review_status=(
            ReviewStatus.PENDING_REVIEW.value if pending_review else ReviewStatus.NOT_REQUIRED.value
        ),
        validation_status=ValidationStatus.VALIDATED.value,
        reason="客观题按既定规则判定。",
        knowledge_points=["导数定义"],
        missing_knowledge_points=[],
        correct_points=["导数定义"],
        suggestions=[],
        retrieved_context_ids=[],
        confidence=None,
        submission_id="submission-t211",
        decision=None,
    )
    items = [counted, second]
    if pending_review:
        result = ExamResultDTO(
            submission_id="submission-t211",
            exam_id="exam-t211",
            student_id="student-t211",
            result_status=ExamResultStatus.PENDING_REVIEW,
            is_final=False,
            final_total_score=None,
            confirmed_subtotal=Decimal("8.50"),
            confirmed_subtotal_label="已确认部分小计：8.50",
            total_max_score=Decimal("15.00"),
            expected_answer_count=2,
            graded_answer_count=2,
            counted_answer_count=1,
            pending_review_answer_count=1,
            items=items,
            aggregated_at=datetime(2026, 10, 17, 2, 0, tzinfo=UTC),
        )
    else:
        result = ExamResultDTO(
            submission_id="submission-t211",
            exam_id="exam-t211",
            student_id="student-t211",
            result_status=ExamResultStatus.FINAL,
            is_final=True,
            final_total_score=Decimal("11.00"),
            confirmed_subtotal=Decimal("11.00"),
            confirmed_subtotal_label="已确认部分小计：11.00",
            total_max_score=Decimal("15.00"),
            expected_answer_count=2,
            graded_answer_count=2,
            counted_answer_count=2,
            pending_review_answer_count=0,
            items=items,
            aggregated_at=datetime(2026, 10, 17, 2, 0, tzinfo=UTC),
        )
    return items, result


def sample_state(
    *,
    pending_review: bool = False,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """一份旧版形状（v2，无 snapshot_id）或带身份的完整工作流状态。"""

    items, result = business_items(pending_review=pending_review)
    state: dict[str, Any] = {
        "workflow_id": "workflow-t211",
        "request_id": "request-t211",
        "submission_id": "submission-t211",
        "status": WorkflowStatus.PAUSED if pending_review else WorkflowStatus.COMPLETED,
        "current_node": "await_teacher_review" if pending_review else "generate_diagnosis",
        "current_answer_id": "answer-t211-2",
        "current_answer_order": 2,
        "retry_count": 0,
        "pause_reason": "存在待人工复核题目。" if pending_review else None,
        "resumable": bool(pending_review),
        "submission_context": sample_context(),
        "question_type": QuestionType.SINGLE_CHOICE,
        "query": "【题目】导数定义",
        "retrieved_context_ids": ["chunk-1"] if not pending_review else [],
        "grading_results": {},
        "scoring_inputs": {},
        "validation_status": ValidationStatus.VALIDATED,
        "confidence": None if pending_review else 0.9,
        "confidence_decisions": {},
        "review_status": (
            ReviewStatus.PENDING_REVIEW if pending_review else ReviewStatus.NOT_REQUIRED
        ),
        "final_results": items,
        "exam_result": result,
        "diagnosis": None,
        "error": None,
    }
    if snapshot_id is not None:
        state["snapshot_id"] = snapshot_id
    return state


def checkpoint_payload(state: Mapping[str, Any], *, version: str) -> dict[str, Any]:
    """按给定版本封装状态正文；正文由当前编解码校验，不手写字段表。"""

    return {
        "kind": WORKFLOW_STATE_PAYLOAD_KIND,
        "version": version,
        CHECKPOINT_STATE_KEY: workflow_state_to_json(state),
    }


def legacy_payload(state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """T212 之前的载荷版本（v2），编码器与当前实现一致。"""

    return checkpoint_payload(state if state is not None else sample_state(), version="2")


def current_payload(state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return checkpoint_payload(state if state is not None else sample_state(), version=WORKFLOW_STATE_PAYLOAD_VERSION)


NEW_RUN_SNAPSHOT_ENV = "t211-new-run-snapshot-id"


# ---------------------------------------------------------------------------
# 索引身份端口与 Fake（T214）
# ---------------------------------------------------------------------------


def index_service(repository: Any, *, clock: Any = None, principal: Any = None) -> Any:
    factory = required_symbol(INDEX_MODULE, "IndexIdentityService", T214)
    kwargs: dict[str, Any] = {"repository": repository}
    if clock is not None:
        kwargs["clock"] = clock
    if principal is not None:
        kwargs["principal"] = principal
    return factory(**kwargs)


def index_operation(service: Any, name: str) -> Any:
    return required_method(service, name, T214)


def identity_view(service: Any, kb_id: UUID) -> dict[str, Any]:
    return facts_of(index_operation(service, "view")(kb_id=kb_id))


def preview_adoption(
    service: Any,
    *,
    kb_id: UUID,
    expected_identity_revision: Any,
    proposed_identity: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    operator_attestation: Mapping[str, Any],
    management_session_id: str,
) -> dict[str, Any]:
    return facts_of(
        index_operation(service, "preview_adoption")(
            kb_id=kb_id,
            expected_identity_revision=expected_identity_revision,
            proposed_identity=dict(proposed_identity),
            evidence=[dict(item) for item in evidence],
            operator_attestation=dict(operator_attestation),
            management_session_id=management_session_id,
        )
    )


def confirm_adoption(
    service: Any,
    *,
    preview_id: Any,
    expected_identity_revision: Any,
    confirmation_id: UUID,
    management_session_id: str,
    confirm: bool = True,
) -> dict[str, Any]:
    return facts_of(
        index_operation(service, "confirm_adoption")(
            preview_id=preview_id,
            expected_identity_revision=expected_identity_revision,
            confirmation_id=confirmation_id,
            management_session_id=management_session_id,
            confirm=confirm,
        )
    )


def register_native(
    service: Any,
    *,
    kb_id: UUID,
    identity: Mapping[str, Any],
    unit_of_work: Any,
) -> Any:
    return index_operation(service, "register_native")(
        kb_id=kb_id,
        identity=dict(identity),
        unit_of_work=unit_of_work,
    )


def require_compatible(service: Any, *, kb_id: UUID, identity: Mapping[str, Any]) -> Any:
    return index_operation(service, "require_compatible")(kb_id=kb_id, identity=dict(identity))


def proposed_identity(
    *,
    provider: str = "local",
    adapter: str = "local",
    endpoint_identity: str = "local://t211",
    model_id: str = "t211-embedding-a",
    model_version: str | None = None,
    dimension: int = 1024,
    semantic_parameters: Mapping[str, Any] | None = None,
    identity_revision: int = 1,
) -> dict[str, Any]:
    """data-model §7 的身份对象；不含任何秘密。"""

    return {
        "provider": provider,
        "adapter": adapter,
        "endpoint_identity": endpoint_identity,
        "model_id": model_id,
        "model_version": model_version,
        "dimension": dimension,
        "semantic_parameters": dict(semantic_parameters or {"normalize": True, "document_prefix": ""}),
        "identity_revision": identity_revision,
    }


def evidence_record(
    kb_id: UUID,
    *,
    kind: str = "ingestion_record",
    model_id: str = "t211-embedding-a",
    dimension: int = 1024,
    **changes: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": kind,
        "source_reference": f"t211/{kind}/{uuid4().hex}",
        "source_date": "2025-06-01",
        "kb_scope": str(kb_id),
        "safe_excerpt": "合成脱敏摘录：摄取记录显示 provider=local, model=t211-embedding-a。",
        "supported_fields": {
            "provider": "local",
            "adapter": "local",
            "model_id": model_id,
            "dimension": dimension,
            "semantic_parameters": {"normalize": True, "document_prefix": ""},
        },
    }
    record.update(changes)
    return record


def attestation(*, covers: str = "本次接纳所依据的原始摄取记录与本KB全部chunk") -> dict[str, Any]:
    return {"verified": True, "covers": covers}


class FakePrincipal:
    """服务端操作者事实；请求体不能覆盖，这里也不判断权限。"""

    def __init__(
        self,
        *,
        principal_kind: str = "local_os",
        principal_id: str = "t211-local-operator",
        environment_id: UUID | None = None,
        instance_id: str = "t211-instance",
        management_session_id: str = "t211-session-1",
    ) -> None:
        self.facts = {
            "principal_kind": principal_kind,
            "principal_id": principal_id,
            "environment_id": str(environment_id) if environment_id else str(uuid4()),
            "instance_id": instance_id,
            "management_session_id": management_session_id,
        }

    def current(self) -> dict[str, Any]:
        return dict(self.facts)

    def session_id(self) -> str:
        return self.facts["management_session_id"]

    def __call__(self) -> dict[str, Any]:  # 兼容按调用取事实的实现
        return self.current()


@dataclass
class FakeKbRow:
    """KB 端口事实：identity 为 None 表示历史 NULL（读取映射由产品决定）。"""

    kb_id: UUID
    identity: dict[str, Any] | None = None
    identity_revision: int | None = None
    adoption: dict[str, Any] | None = None
    audits: list[dict[str, Any]] = field(default_factory=list)
    chunks: dict[str, tuple[float, ...]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    commit_count: int = 0
    fail_next_commit: bool = False
    writes_without_audit: int = 0


class KbLockUnavailable(RuntimeError):
    """端口级行锁争用事实；产品必须自行决定如何安全报告冲突。"""


class FakeUnitOfWork:
    """KB 行锁 + 单次提交边界；端口只暂存与提交，不做任何身份决策。"""

    def __init__(self, repository: FakeKnowledgeBaseRepository, kb_id: UUID, *, timeout: float = 5.0) -> None:
        self._repository = repository
        self._row = repository.row(kb_id)
        self._timeout = timeout
        self._staged: dict[str, Any] = {}
        self._locked = False
        self._committed = False

    def __enter__(self) -> Self:
        if not self._row.lock.acquire(timeout=self._timeout):
            raise KbLockUnavailable("synthetic KB row lock is held by another writer")
        self._locked = True
        return self

    def __exit__(self, *_: object) -> None:
        if self._locked:
            self._locked = False
            self._row.lock.release()

    def read(self) -> dict[str, Any]:
        """锁内重查事实：身份、修订、向量指纹与既有接纳。"""

        return {
            "kb_id": self._row.kb_id,
            "identity": None if self._row.identity is None else dict(self._row.identity),
            "identity_revision": self._row.identity_revision,
            "adoption": None if self._row.adoption is None else dict(self._row.adoption),
            "vector_fingerprint": vector_fingerprint(self._repository, self._row.kb_id),
            "stored_dimension": self._repository.stored_dimension(self._row.kb_id),
            "chunk_count": len(self._row.chunks),
        }

    def write_vectors(self, chunks: Mapping[str, Sequence[float]]) -> None:
        """摄取侧在同一个事务里写向量；端口只暂存。"""

        staged: dict[str, tuple[float, ...]] = self._staged.setdefault("vectors", {})
        staged.update({str(name): tuple(float(item) for item in vector) for name, vector in chunks.items()})

    def write_identity(self, identity: Mapping[str, Any] | None = None, **extra: Any) -> None:
        if identity is not None:
            self._staged["identity"] = dict(identity)
        for name, value in extra.items():
            self._staged[name] = value

    def write_adoption(self, adoption: Mapping[str, Any], **extra: Any) -> None:
        self._staged["adoption"] = dict(adoption)
        for name, value in extra.items():
            self._staged[name] = value

    def write_audit(self, audit: Mapping[str, Any]) -> None:
        self._staged.setdefault("audits", []).append(dict(audit))

    def commit(self) -> None:
        if self._row.fail_next_commit:
            self._row.fail_next_commit = False
            raise RuntimeError("synthetic commit failure")
        has_adoption = "adoption" in self._staged or bool(self._staged.get("audits"))
        if "identity" in self._staged and not has_adoption and self._row.identity is not None:
            # 变更既有身份却没有审计：端口记录该事实，测试据此判定原子性。
            self._row.writes_without_audit += 1
        if "identity" in self._staged:
            self._row.identity = self._staged["identity"]
            revision = self._staged["identity"].get("identity_revision")
            self._row.identity_revision = int(revision) if revision is not None else self._row.identity_revision
        if "adoption" in self._staged:
            self._row.adoption = self._staged["adoption"]
        for name, vector in self._staged.get("vectors", {}).items():
            self._row.chunks[name] = tuple(vector)
        for audit in self._staged.get("audits", []):
            self._row.audits.append(audit)
        self._row.commit_count += 1
        self._committed = True
        self._staged = {}


class FakeKnowledgeBaseRepository:
    """只存储事实、提供行锁与故障注入；判断留给产品服务。"""

    def __init__(self) -> None:
        self.rows: dict[UUID, FakeKbRow] = {}

    def add(
        self,
        kb_id: UUID | None = None,
        *,
        identity: Mapping[str, Any] | None = None,
        chunks: Mapping[str, Sequence[float]] | None = None,
    ) -> FakeKbRow:
        row = FakeKbRow(
            kb_id=kb_id or uuid4(),
            identity=None if identity is None else dict(identity),
            identity_revision=(int(identity["identity_revision"]) if identity and "identity_revision" in identity else None),
            chunks={str(name): tuple(float(item) for item in vector) for name, vector in (chunks or {}).items()},
        )
        self.rows[row.kb_id] = row
        return row

    def row(self, kb_id: UUID) -> FakeKbRow:
        return self.rows[kb_id]

    def begin(self, kb_id: UUID, *, timeout: float = 5.0) -> FakeUnitOfWork:
        return FakeUnitOfWork(self, kb_id, timeout=timeout)

    def row_lock(self, kb_id: UUID) -> FakeUnitOfWork:
        """`begin` 的别名，便于实现方选择命名。"""

        return self.begin(kb_id)

    def fail_next_commit(self, kb_id: UUID) -> None:
        self.row(kb_id).fail_next_commit = True

    def stored_dimension(self, kb_id: UUID) -> int | None:
        """实际存储的向量维度；无向量时为 None，不猜默认值。"""

        row = self.row(kb_id)
        if not row.chunks:
            return None
        return len(next(iter(row.chunks.values())))

    def read(self, kb_id: UUID) -> dict[str, Any]:
        return self.facts(kb_id)

    def facts(self, kb_id: UUID) -> dict[str, Any]:
        row = self.row(kb_id)
        return {
            "identity": None if row.identity is None else dict(row.identity),
            "identity_revision": row.identity_revision,
            "adoption": None if row.adoption is None else dict(row.adoption),
            "audits": list(row.audits),
            "commit_count": row.commit_count,
            "writes_without_audit": row.writes_without_audit,
            "vector_fingerprint": vector_fingerprint(self, kb_id),
            "stored_dimension": self.stored_dimension(kb_id),
            "chunk_count": len(row.chunks),
        }


def vector_fingerprint(repository: FakeKnowledgeBaseRepository, kb_id: UUID) -> tuple[Any, ...]:
    row = repository.row(kb_id)
    return tuple(sorted((name, tuple(vector)) for name, vector in row.chunks.items()))


def synthetic_vector(seed: int = 1, *, dimension: int = 1024) -> tuple[float, ...]:
    return tuple(round(((seed + index) % 17) / 17.0, 6) for index in range(dimension))


def run_concurrently(*workers: Any) -> list[Any]:
    """并发执行端口层场景；异常作为结果返回，便于断言冲突码而不是崩溃。"""

    def guarded(worker: Any) -> Any:
        try:
            return worker()
        except BaseException as error:  # noqa: BLE001 - 测试要把冲突当结果断言
            return error

    with ThreadPoolExecutor(max_workers=max(2, len(workers))) as pool:
        futures = [pool.submit(guarded, worker) for worker in workers]
        return [future.result() for future in futures]


__all__ = [
    "CONFLICT_CODES",
    "EMBEDDING_INCOMPATIBLE",
    "EVIDENCE_CONFLICT",
    "EVIDENCE_INSUFFICIENT",
    "IDENTITY_CONFLICT",
    "INDEX_BUSY",
    "INDEX_MODULE",
    "LEGACY_ADOPTED",
    "LEGACY_UNKNOWN",
    "NATIVE",
    "NEW_RUN_SNAPSHOT_ENV",
    "PREVIEW_EXPIRED",
    "SNAPSHOT_DEPENDENCY_UNAVAILABLE",
    "SNAPSHOT_MODULE",
    "SNAPSHOT_UNAVAILABLE",
    "T212",
    "T214",
    "Admission",
    "FakeClock",
    "FakeKbRow",
    "FakeKnowledgeBaseRepository",
    "FakePrincipal",
    "FakeUnitOfWork",
    "KbLockUnavailable",
    "SettingsHarness",
    "accepts_keyword",
    "ai_values",
    "assert_no_secret",
    "attestation",
    "business_items",
    "checkpoint_payload",
    "confirm_adoption",
    "create_snapshot",
    "credential_state",
    "current_payload",
    "evidence_record",
    "facts_of",
    "facts_text",
    "identity_view",
    "index_operation",
    "index_service",
    "legacy_payload",
    "legacy_run_fact",
    "method",
    "preview_adoption",
    "proposed_identity",
    "register_native",
    "require_compatible",
    "required_method",
    "required_symbol",
    "resolve_snapshot",
    "run_concurrently",
    "safe_code",
    "sample_context",
    "sample_state",
    "settings_harness",
    "snapshot_facts",
    "snapshot_files",
    "snapshot_root",
    "snapshot_service",
    "synthetic_vector",
    "value_of",
    "vector_fingerprint",
    "wire_admission",
]
