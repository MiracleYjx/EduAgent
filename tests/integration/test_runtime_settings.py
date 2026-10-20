"""TCR-S5-RUNTIME-SNAPSHOT-INDEX-013：T211 运行快照、跨重启恢复与旧版状态兼容。

T212（不可变快照/旧版状态兼容）尚未实现：需要快照服务的用例在 call 阶段以
``T212_IMPLEMENTATION_REQUIRED`` 显式红灯，其余用例固定当前已成立的原业务事实
（待复核不计入总分、最终成绩一致性、分值往返），确保本轮只新增断言、不改产品。

边界：只用 FakeProtector/MockTransport 与 ``tmp_path`` 隔离目录；不读真实凭据、
不连真实 PG/Redis、不调用付费模型、不操作 Development/Demo 数据。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.ai.workflows.grading_workflow import GradingWorkflow
from backend.app.ai.workflows.state import (
    CHECKPOINT_STATE_KEY,
    WORKFLOW_STATE_PAYLOAD_KIND,
    WORKFLOW_STATE_PAYLOAD_VERSION,
    WorkflowStateError,
    workflow_state_from_checkpoint_payload,
    workflow_state_to_checkpoint_payload,
    workflow_state_to_json,
)
from backend.app.api.workflow import WorkflowService
from backend.app.domain.enums import ReviewStatus, UserRole, WorkflowStatus
from backend.app.models import WorkflowRun
from backend.app.schemas.grading import ExamResultDTO, ExamResultStatus
from backend.app.schemas.storage_maintenance import BackupFile, BackupManifest
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
)
from backend.app.services.settings.credential_store import CredentialStoreError
from backend.app.services.settings.runtime_snapshot import (
    RuntimeSnapshotService,
    SnapshotReferenceIndex,
    legacy_run_index,
)
from backend.app.services.workflow_checkpoint import (
    WorkflowCheckpointError,
    WorkflowCheckpointStore,
    pending_review_answer_ids,
    snapshot_bindings_from_rows,
)
from tests.integration import test_runtime_consumers as t213
from tests.integration.test_runtime_consumers import _StubAgent, _StubDiagnosis
from tests.support import runtime_consumer_fakes as consumer_fakes
from tests.support import runtime_settings_fakes as t211
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.unit.models.sqlite_support import seed_submission
from tests.unit.settings_helpers import build_test_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD_SOURCE = textwrap.dedent(
    r"""
    import json
    import sys
    from pathlib import Path
    from uuid import UUID

    sys.path.insert(0, "@REPO@")

    from backend.app.services.settings.config_service import ConfigService
    from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService

    root, environment_id, snapshot_id = sys.argv[1], UUID(sys.argv[2]), sys.argv[3]
    config = ConfigService(Path(root), environment_id, writable=False)
    resolved = RuntimeSnapshotService(config).resolve_for_state(
        {"workflow_id": "wf-t211-child", "snapshot_id": snapshot_id}
    )
    snapshot = resolved["snapshot"] if isinstance(resolved, dict) else resolved.snapshot
    facts = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
    print("T211_CHILD_FACTS=" + json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str))
    """
).replace("@REPO@", str(REPO_ROOT))


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[t211.SettingsHarness]:
    built = t211.settings_harness(tmp_path)
    try:
        yield built
    finally:
        built.close()


def _activate(harness: t211.SettingsHarness, model_id: str, **kwargs: Any) -> Any:
    return asyncio.run(harness.activate_model(model_id, **kwargs))


# ---------------------------------------------------------------------------
# 原业务断言（当前实现即应成立，不允许为全绿而放宽）
# ---------------------------------------------------------------------------


def test_pending_review_item_is_not_counted_and_blocks_finalization() -> None:
    items, result = t211.business_items(pending_review=True)
    pending = items[1]
    assert pending.requires_review is True
    assert pending.counted is False
    assert pending.effective_score is None
    assert pending.decision is None  # 客观题不产生模型决策
    assert result.is_final is False
    assert result.final_total_score is None
    assert result.pending_review_answer_count == 1
    assert result.counted_answer_count == 1

    final = t211.business_items()[1]
    assert final.is_final is True and final.final_total_score is not None
    dump = final.model_dump()
    with pytest.raises(ValidationError):
        ExamResultDTO(**{**dump, "is_final": False})
    with pytest.raises(ValidationError):
        ExamResultDTO(**{**dump, "final_total_score": None})
    with pytest.raises(ValidationError):
        ExamResultDTO(**{**dump, "result_status": ExamResultStatus.PENDING_REVIEW})


def test_pending_review_state_cannot_claim_completion() -> None:
    state = t211.sample_state(pending_review=True)
    payload = workflow_state_to_checkpoint_payload(state)
    assert payload["state"]["review_status"] == ReviewStatus.PENDING_REVIEW.value
    echoed = workflow_state_to_json(state)
    assert echoed["exam_result"]["is_final"] is False
    with pytest.raises(WorkflowStateError):
        workflow_state_to_json({**state, "status": WorkflowStatus.COMPLETED})


def test_legacy_payload_round_trip_preserves_scores_and_review_gates() -> None:
    payload = t211.legacy_payload(t211.sample_state(pending_review=True))
    assert payload["version"] == "2"
    restored = workflow_state_from_checkpoint_payload(json.loads(json.dumps(payload)))
    assert "snapshot_id" not in restored
    assert restored["review_status"] is ReviewStatus.PENDING_REVIEW
    assert restored["exam_result"] is not None and restored["exam_result"].is_final is False
    totals = {item.answer_id: item.effective_score for item in restored["final_results"]}
    assert totals == {"answer-t211-1": Decimal("8.50"), "answer-t211-2": None}
    assert WORKFLOW_STATE_PAYLOAD_VERSION  # 版本常量仍由产品模块持有


def test_no_second_workflow_state_table_exists() -> None:
    import backend.app.models  # noqa: F401  仅用于填充 SQLAlchemy 元数据
    from backend.app.models.base import Base

    tables = {name for name in Base.metadata.tables if "workflow" in name or "snapshot" in name}
    assert tables == {"workflow_runs"}


# ---------------------------------------------------------------------------
# T212 快照服务（本轮显式红灯）
# ---------------------------------------------------------------------------


def test_snapshot_records_resolved_bindings_without_secrets(harness: t211.SettingsHarness) -> None:
    revision = _activate(harness, "t211-model-a", with_credential=True)
    service = t211.snapshot_service(harness.service)
    snapshot = t211.create_snapshot(service, workflow_id="wf-t211-record")
    facts = t211.snapshot_facts(snapshot)
    assert str(t211.value_of(facts, "settings_revision")) == str(revision.revision_id)
    for name in (
        "resolved_task_bindings",
        "provider_identity",
        "prompt_versions",
        "schema_versions",
        "retrieval_policy",
        "confidence_policy",
        "embedding_identity",
        "credential_refs",
        "created_at",
        "legacy_origin",
    ):
        t211.value_of(facts, name)
    assert str(facts["legacy_origin"] or "").lower() != t211.LEGACY_UNKNOWN
    text = t211.facts_text(snapshot)
    assert "t211-model-a" in text and "openai_compatible" in text
    stored = t211.snapshot_files(harness.root)
    assert stored, "运行快照必须持久于 config/snapshots"
    assert any(str(facts["snapshot_id"]) in Path(name).stem for name in stored)
    secret = harness.store.get(harness.provider_reference, harness.provider_reference.version)
    t211.assert_no_secret(
        [secret.get_secret_value()],
        {
            "snapshot": text,
            "files": json.dumps({name: body.decode("utf-8", "replace") for name, body in stored.items()}),
        },
    )


def test_activation_switch_keeps_in_flight_binding_and_new_runs_use_new_binding(
    harness: t211.SettingsHarness,
) -> None:
    revision_a = _activate(harness, "t211-model-a")
    service = t211.snapshot_service(harness.service)
    in_flight = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t211-in-flight"))
    saved = t211.facts_text(in_flight)

    revision_b = _activate(harness, "t211-model-b", expected=revision_a.revision_id)
    assert harness.service.read_manifest().active_revision == revision_b.revision_id

    resolved = t211.resolve_snapshot(service, t211.sample_state(snapshot_id=in_flight["snapshot_id"]))
    assert resolved["origin"] == t211.NATIVE
    assert t211.facts_text(resolved["snapshot"]) == saved
    assert "t211-model-b" not in saved

    fresh = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t211-new"))
    fresh_text = t211.facts_text(fresh)
    assert fresh["snapshot_id"] != in_flight["snapshot_id"]
    assert "t211-model-b" in fresh_text and "t211-model-a" not in fresh_text


def test_restart_resolution_uses_saved_values_not_current_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = t211.settings_harness(tmp_path)
    try:
        revision_a = _activate(harness, "t211-model-a", with_credential=True)
        service = t211.snapshot_service(harness.service)
        snapshot = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t211-restart"))
        saved = t211.facts_text(snapshot)
    finally:
        harness.close()

    monkeypatch.setenv("EDUAGENT__POLICIES__CONFIDENCE_THRESHOLD", "0.99")
    monkeypatch.setenv("EDUAGENT__BUSINESS__JWT_EXPIRE_MINUTES", "5")

    restarted = harness.reopen()
    try:
        # 重启后只换模型与策略：业务段不变，避免把 JWT 引用变化误判为需要重启。
        restarted_values = revision_a.values.model_dump(mode="json")
        profile_key = next(iter(restarted_values["providers"]))
        new_revision = str(uuid4())
        restarted_values["providers"][profile_key]["model_id"] = "t211-model-b"
        restarted_values["providers"][profile_key]["profile_revision"] = new_revision
        restarted_values["bindings"]["default_llm"]["profile_revision"] = new_revision
        restarted_values["policies"]["confidence_threshold"] = 0.99
        restarted_draft = restarted.draft(restarted_values, expected=revision_a.revision_id)
        revision_b = asyncio.run(
            restarted.activate(restarted_draft, expected=revision_a.revision_id)
        )
        assert restarted.service.read_manifest().active_revision == revision_b.revision_id
        restarted_service = t211.snapshot_service(restarted.service)
        resolved = t211.resolve_snapshot(
            restarted_service, t211.sample_state(snapshot_id=snapshot["snapshot_id"])
        )
        assert resolved["origin"] == t211.NATIVE
        assert t211.facts_text(resolved["snapshot"]) == saved
        assert "0.99" not in t211.facts_text(resolved["snapshot"])
    finally:
        restarted.close()


def test_missing_snapshot_reports_snapshot_unavailable(harness: t211.SettingsHarness) -> None:
    revision = _activate(harness, "t211-model-a")
    service = t211.snapshot_service(harness.service)
    requests_before = len(harness.admission.requests)
    with pytest.raises(Exception) as error:
        t211.resolve_snapshot(service, t211.sample_state(snapshot_id=str(uuid4())))
    assert t211.safe_code(error.value) == t211.SNAPSHOT_UNAVAILABLE
    assert t211.snapshot_files(harness.root) == {}, "缺失快照不得用当前配置补造"
    assert harness.service.read_manifest().active_revision == revision.revision_id
    assert len(harness.admission.requests) == requests_before


def test_legacy_state_is_legacy_unknown_without_fabricated_snapshot(
    harness: t211.SettingsHarness,
) -> None:
    revision = _activate(harness, "t211-model-a")
    service = t211.snapshot_service(harness.service)
    requests_before = len(harness.admission.requests)
    resolved = t211.resolve_snapshot(service, t211.sample_state())
    assert resolved["origin"] == t211.LEGACY_UNKNOWN
    assert resolved["snapshot"] is None
    assert t211.snapshot_files(harness.root) == {}
    assert harness.service.read_manifest().active_revision == revision.revision_id
    assert len(harness.admission.requests) == requests_before


def test_revoked_credential_blocks_snapshot_dependency_without_model_call(
    harness: t211.SettingsHarness,
) -> None:
    revision_a = _activate(harness, "t211-model-a", with_credential=True)
    service = t211.snapshot_service(harness.service)
    snapshot = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t211-revoked"))
    reference = harness.provider_reference
    assert t211.credential_state(harness.store, reference) == "available"
    harness.store.revoke(reference, reference.version)
    assert t211.credential_state(harness.store, reference) == "revoked"

    requests_before = len(harness.admission.requests)
    with pytest.raises(Exception) as error:
        t211.resolve_snapshot(service, t211.sample_state(snapshot_id=snapshot["snapshot_id"]))
    assert t211.safe_code(error.value) == t211.SNAPSHOT_DEPENDENCY_UNAVAILABLE
    assert len(harness.admission.requests) == requests_before
    assert harness.service.read_manifest().active_revision == revision_a.revision_id
    stored = t211.snapshot_files(harness.root)
    assert stored, "撤销凭据不得删除原快照事实"
    t211.assert_no_secret(
        ["t211_synthetic_provider_"],
        {"error": str(error.value), "files": json.dumps(sorted(stored))},
    )


def test_cross_process_restore_reads_saved_snapshot_facts(
    harness: t211.SettingsHarness, tmp_path: Path
) -> None:
    _activate(harness, "t211-model-a")
    service = t211.snapshot_service(harness.service)
    snapshot = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t211-cross-process"))
    expected = t211.facts_text(snapshot)

    script = tmp_path / "t211_snapshot_child.py"
    script.write_text(_CHILD_SOURCE, encoding="utf-8")
    env = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC")
        if name in os.environ
    }
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "EDUAGENT__POLICIES__CONFIDENCE_THRESHOLD": "0.99",
            "EDUAGENT__BUSINESS__JWT_EXPIRE_MINUTES": "5",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(script),
            str(harness.root),
            str(harness.environment_id),
            str(snapshot["snapshot_id"]),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    rows = [
        row for row in completed.stdout.splitlines() if row.startswith("T211_CHILD_FACTS=")
    ]
    assert rows, completed.stdout[-2000:]
    child_facts = json.loads(rows[-1].split("=", 1)[1])
    assert json.dumps(child_facts, ensure_ascii=False, sort_keys=True, default=str) == expected


def test_legacy_nonterminal_run_blocks_model_and_policy_activation(
    harness: t211.SettingsHarness,
) -> None:
    revision_a = _activate(harness, "t211-model-a")
    runs = [t211.legacy_run_fact("wf-t211-legacy-1")]
    t211.wire_admission(
        harness.service,
        harness.admission,
        business_guard=harness.business_guard,
        legacy_runs=lambda: tuple(runs),
        clock=harness.clock,
    )

    model_draft = harness.draft(harness.values("t211-model-b"), expected=revision_a.revision_id)
    with pytest.raises(Exception) as model_error:
        asyncio.run(harness.activate(model_draft, expected=revision_a.revision_id))
    assert "LEGACY" in t211.safe_code(model_error.value).upper()
    assert harness.service.read_manifest().active_revision == revision_a.revision_id

    policy_values = revision_a.values.model_dump(mode="json")
    policy_values["policies"]["confidence_threshold"] = 0.55
    policy_draft = harness.draft(policy_values, expected=revision_a.revision_id)
    with pytest.raises(Exception) as policy_error:
        asyncio.run(harness.activate(policy_draft, expected=revision_a.revision_id))
    assert "LEGACY" in t211.safe_code(policy_error.value).upper()
    assert harness.service.read_manifest().active_revision == revision_a.revision_id


def test_legacy_gate_clears_when_run_reaches_terminal_state(harness: t211.SettingsHarness) -> None:
    revision_a = _activate(harness, "t211-model-a")
    runs = [t211.legacy_run_fact("wf-t211-legacy-2")]
    t211.wire_admission(
        harness.service,
        harness.admission,
        business_guard=harness.business_guard,
        legacy_runs=lambda: tuple(runs),
        clock=harness.clock,
    )
    draft = harness.draft(harness.values("t211-model-b"), expected=revision_a.revision_id)
    with pytest.raises(Exception) as error:
        asyncio.run(harness.activate(draft, expected=revision_a.revision_id))
    assert "LEGACY" in t211.safe_code(error.value).upper()

    runs.clear()
    revision_b = asyncio.run(harness.activate(draft, expected=revision_a.revision_id))
    assert harness.service.read_manifest().active_revision == revision_b.revision_id
    model_ids = {profile.model_id for profile in revision_b.values.providers.values()}
    assert model_ids == {"t211-model-b"}


def test_unrelated_business_change_is_not_blocked_by_legacy_gate(harness: t211.SettingsHarness) -> None:
    revision_a = _activate(harness, "t211-model-a")
    t211.wire_admission(
        harness.service,
        harness.admission,
        business_guard=harness.business_guard,
        legacy_runs=lambda: (t211.legacy_run_fact("wf-t211-legacy-3"),),
        clock=harness.clock,
    )
    values = revision_a.values.model_dump(mode="json")
    values["business"]["jwt_expire_minutes"] = int(values["business"].get("jwt_expire_minutes", 60)) + 5
    candidate = harness.draft(values, expected=revision_a.revision_id)
    revision = harness.service.activate(
        candidate.revision_id, expected_revision=revision_a.revision_id, process_env={}
    )
    assert revision.status == "pending_restart"
    assert harness.service.read_manifest().active_revision == revision_a.revision_id
    assert harness.service.read_manifest().pending_restart_revision == revision.revision_id


# ---------------------------------------------------------------------------
# T212：客户端租约、非终态引用事实源与快照备份承载
# ---------------------------------------------------------------------------


class _LeaseClient:
    """只记录关闭次数的客户端替身；不执行任何模型/网络行为。"""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _lease(service: Any, snapshot_id: Any, task_kind: str, created: list[str]) -> Any:
    def factory(binding: Any) -> _LeaseClient:
        created.append(binding.task_kind)
        return _LeaseClient()

    return service.acquire(snapshot_id=snapshot_id, task_kind=task_kind, factory=factory)


def test_snapshot_client_lease_is_reused_within_one_snapshot(
    harness: t211.SettingsHarness,
) -> None:
    _activate(harness, "t211-model-a")
    service = t211.snapshot_service(harness.service)
    snapshot = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t212-lease"))
    created: list[str] = []
    first = _lease(service, snapshot["snapshot_id"], "subjective_grading", created)
    second = _lease(service, snapshot["snapshot_id"], "subjective_grading", created)
    assert first is second
    assert first.client is second.client
    assert created == ["subjective_grading"], "同快照同任务必须复用同一客户端"
    first.release()
    first.release()
    assert first.released is True
    assert first.client.closed == 1, "释放必须幂等，不重复关闭"


def test_snapshot_client_lease_is_not_reused_across_snapshots(
    harness: t211.SettingsHarness,
) -> None:
    _activate(harness, "t211-model-a")
    service = t211.snapshot_service(harness.service)
    first_snapshot = t211.snapshot_facts(
        t211.create_snapshot(service, workflow_id="wf-t212-lease-a")
    )
    second_snapshot = t211.snapshot_facts(
        t211.create_snapshot(service, workflow_id="wf-t212-lease-b")
    )
    created: list[str] = []
    first = _lease(service, first_snapshot["snapshot_id"], "subjective_grading", created)
    second = _lease(service, second_snapshot["snapshot_id"], "subjective_grading", created)
    assert first.client is not second.client, "不得跨快照（含同修订新快照）复用客户端"
    assert len(created) == 2
    assert service.release_all(first_snapshot["snapshot_id"]) == 1
    assert first.released is True and second.released is False
    service.close()
    service.close()
    assert second.released is True
    assert second.client.closed == 1


def test_snapshot_client_lease_reports_unknown_binding(
    harness: t211.SettingsHarness,
) -> None:
    _activate(harness, "t211-model-a")
    service = t211.snapshot_service(harness.service)
    snapshot = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t212-lease-c"))
    with pytest.raises(Exception) as error:
        _lease(service, snapshot["snapshot_id"], "vision", [])
    assert t211.safe_code(error.value) == "SNAPSHOT_BINDING_UNAVAILABLE"


def _nonterminal_binding(
    workflow_id: str, snapshot_id: str | None, status: WorkflowStatus = WorkflowStatus.RUNNING
) -> Any:
    payload = {
        "kind": WORKFLOW_STATE_PAYLOAD_KIND,
        "version": WORKFLOW_STATE_PAYLOAD_VERSION,
        CHECKPOINT_STATE_KEY: {"snapshot_id": snapshot_id} if snapshot_id else {},
    }
    return snapshot_bindings_from_rows([(workflow_id, status, payload)])[0]


def test_snapshot_reference_keeps_old_credential_in_use(
    harness: t211.SettingsHarness,
) -> None:
    first = _activate(harness, "t211-model-a", with_credential=True)
    service = t211.snapshot_service(harness.service)
    snapshot = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t212-ref"))
    old_reference = harness.provider_reference
    _activate(harness, "t211-model-b", expected=first.revision_id, with_credential=True)
    bindings = [_nonterminal_binding("wf-t212-ref", snapshot["snapshot_id"])]
    t211.wire_admission(
        harness.service,
        harness.admission,
        clock=harness.clock,
        runtime_references=SnapshotReferenceIndex(service, lambda: bindings),
    )
    assert harness.service.references(old_reference, old_reference.version) == ("snapshot",)
    with pytest.raises(CredentialStoreError) as error:
        harness.store.delete(old_reference, old_reference.version)
    assert t211.safe_code(error.value) == "IN_USE"
    assert t211.credential_state(harness.store, old_reference) == "available"


def test_legacy_nonterminal_run_keeps_delete_unknown(
    harness: t211.SettingsHarness,
) -> None:
    first = _activate(harness, "t211-model-a", with_credential=True)
    old_reference = harness.provider_reference
    _activate(harness, "t211-model-b", expected=first.revision_id, with_credential=True)
    service = t211.snapshot_service(harness.service)
    bindings = [_nonterminal_binding("wf-t212-legacy-ref", None)]
    t211.wire_admission(
        harness.service,
        harness.admission,
        clock=harness.clock,
        runtime_references=SnapshotReferenceIndex(service, lambda: bindings),
    )
    assert harness.service.references(old_reference, old_reference.version) == ("legacy_run",)
    with pytest.raises(CredentialStoreError) as error:
        harness.store.delete(old_reference, old_reference.version)
    assert t211.safe_code(error.value) == "IN_USE"
    assert t211.credential_state(harness.store, old_reference) == "available"


def test_reference_index_failure_stays_unknown_and_keeps_credential(
    harness: t211.SettingsHarness,
) -> None:
    first = _activate(harness, "t211-model-a", with_credential=True)
    old_reference = harness.provider_reference
    _activate(harness, "t211-model-b", expected=first.revision_id, with_credential=True)

    def failing(reference: Any, version: Any) -> tuple[str, ...]:
        raise RuntimeError("synthetic reference index failure")

    t211.wire_admission(
        harness.service, harness.admission, clock=harness.clock, runtime_references=failing
    )
    with pytest.raises(Exception) as info:
        harness.service.references(old_reference, old_reference.version)
    assert t211.safe_code(info.value) == "REFERENCE_INDEX_UNAVAILABLE"
    with pytest.raises(CredentialStoreError) as error:
        harness.store.delete(old_reference, old_reference.version)
    assert t211.safe_code(error.value) == "REFERENCE_INDEX_UNAVAILABLE"
    assert t211.credential_state(harness.store, old_reference) == "available"


def test_snapshot_binding_projection_never_guesses() -> None:
    modern = t211.current_payload(t211.sample_state(snapshot_id="snapshot-t212"))
    legacy = t211.legacy_payload(t211.sample_state())
    m3 = {"kind": "background-task-checkpoint", "state": {"snapshot_id": "fabricated"}}
    bindings = snapshot_bindings_from_rows(
        [
            ("wf-modern", WorkflowStatus.RUNNING, modern),
            ("wf-legacy", WorkflowStatus.PAUSED, legacy),
            ("wf-m3", "Running", m3),
        ]
    )
    assert [item.snapshot_id for item in bindings] == ["snapshot-t212", None, None]
    assert [item.status for item in bindings] == ["Running", "Paused", "Running"]
    with pytest.raises(WorkflowCheckpointError):
        snapshot_bindings_from_rows([("  ", WorkflowStatus.RUNNING, None)])


def test_legacy_run_index_projects_only_runs_without_snapshot() -> None:
    modern = t211.current_payload(t211.sample_state(snapshot_id="snapshot-t212"))
    legacy = t211.legacy_payload(t211.sample_state())
    source = legacy_run_index(
        lambda: snapshot_bindings_from_rows(
            [
                ("wf-old", WorkflowStatus.PAUSED, legacy),
                ("wf-new", WorkflowStatus.RUNNING, modern),
            ]
        )
    )
    facts = source()
    assert [fact["workflow_id"] for fact in facts] == ["wf-old"]
    assert facts[0]["snapshot_id"] is None


def test_snapshot_backup_entries_are_verified_and_secret_free(
    harness: t211.SettingsHarness, tmp_path: Path
) -> None:
    _activate(harness, "t211-model-a", with_credential=True)
    service = t211.snapshot_service(harness.service)
    first = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t212-backup-a"))
    second = t211.snapshot_facts(t211.create_snapshot(service, workflow_id="wf-t212-backup-b"))
    destination = tmp_path / "backup-set"
    entries = service.backup_bundle(
        [first["snapshot_id"], second["snapshot_id"]]
    ).copy_entries(destination)
    assert {entry.snapshot_id for entry in entries} == {
        UUID(str(first["snapshot_id"])),
        UUID(str(second["snapshot_id"])),
    }
    service.verify_backup_entries(destination, entries)
    secret = harness.store.get(
        harness.provider_reference, harness.provider_reference.version
    ).get_secret_value()
    for entry in entries:
        content = (destination / entry.relative_path).read_bytes()
        assert len(content) == entry.size_bytes
        assert hashlib.sha256(content).hexdigest() == entry.sha256
        t211.assert_no_secret([secret], {"backup": content.decode("utf-8")})
    target = destination / entries[0].relative_path
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(Exception) as changed:
        service.verify_backup_entries(destination, entries)
    assert t211.safe_code(changed.value) == "SNAPSHOT_BACKUP_CONTENT_CHANGED"
    target.unlink()
    with pytest.raises(Exception) as missing:
        service.verify_backup_entries(destination, entries)
    assert t211.safe_code(missing.value) == "SNAPSHOT_BACKUP_UNAVAILABLE"


def test_snapshot_backup_bundle_refuses_missing_snapshot(
    harness: t211.SettingsHarness, tmp_path: Path
) -> None:
    _activate(harness, "t211-model-a")
    service = t211.snapshot_service(harness.service)
    with pytest.raises(Exception) as error:
        service.backup_bundle([uuid4()]).copy_entries(tmp_path / "empty-set")
    assert t211.safe_code(error.value) == "SNAPSHOT_UNAVAILABLE"


def test_backup_manifest_runtime_snapshots_are_optional_and_scoped() -> None:
    base = BackupManifest(
        backup_set_id=uuid4(),
        outcome="creating",
        started_at=datetime.now(UTC),
        references=[],
        files=[],
        operation_receipts=[],
        issues=[],
    )
    assert base.runtime_snapshots == []
    entry = BackupFile(
        relative_path=f"runtime_snapshots/{uuid4().hex}.json", size_bytes=2, sha256="0" * 64
    )
    accepted = BackupManifest(
        backup_set_id=base.backup_set_id,
        outcome="creating",
        started_at=base.started_at,
        references=[],
        files=[],
        operation_receipts=[],
        issues=[],
        runtime_snapshots=[entry],
    )
    assert accepted.runtime_snapshots[0].relative_path == entry.relative_path
    with pytest.raises(ValidationError):
        BackupManifest(
            backup_set_id=base.backup_set_id,
            outcome="creating",
            started_at=base.started_at,
            references=[],
            files=[],
            operation_receipts=[],
            issues=[],
            runtime_snapshots=[
                BackupFile(relative_path="uploads/x.json", size_bytes=1, sha256="0" * 64)
            ],
        )


# ---------------------------------------------------------------------------
# T215：配置 A/B 切换下的运行级业务闭环
#
# 运行级断言只读既有产品接口（快照解析、图重建、检查点状态与 checkpoints 事实）；
# 合成 SQLite 库、合成答卷与桩 Agent 来自 T213 的同一夹具，不新增产品代码。
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow_env(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """合成工作流库：复用 T213 的文件库构造与同一答卷种子（不新增夹具语义）。"""

    engine = t213._file_engine(tmp_path / "workflow.db")
    with Session(engine) as session:
        fixture = seed_submission(session)
        confirm_synthetic_exam_basis(session, fixture.exam_id)
        session.commit()
        yield {"engine": engine, "fixture": fixture}
    engine.dispose()


def _t215_sessions(env: dict[str, Any]) -> Any:
    """合成工作流库的会话工厂；每个节点使用独立短会话。"""

    def factory() -> Session:
        return Session(env["engine"])

    return factory


def _t215_service(
    env: dict[str, Any], *, agent: Any, runtime_snapshots: Any
) -> WorkflowService:
    """按 T213 的同一装配构建阅卷服务；``None`` 表示未接快照的 legacy 路径。"""

    session_factory = _t215_sessions(env)
    return WorkflowService(
        checkpoints=WorkflowCheckpointStore(session_factory=session_factory),
        reader=DatabaseGradingSubmissionReader(session_factory=session_factory),
        session_factory=session_factory,
        agent=agent,
        diagnosis_service=_StubDiagnosis(),
        repository=DatabaseGradingRepository(session_factory=session_factory),
        settings=build_test_settings(confidence_threshold=0.8),
        runtime_snapshots=runtime_snapshots,
    )


def _t215_runtime(
    harness: t211.SettingsHarness, factory: Any
) -> RuntimeSnapshotService:
    """用隔离设置服务构造快照服务；客户端由记录工厂按快照绑定构造。"""

    return RuntimeSnapshotService(harness.service, client_factory=factory)


def test_activation_switch_keeps_in_flight_run_on_saved_model_and_policy(
    harness: t211.SettingsHarness, workflow_env: dict[str, Any]
) -> None:
    """切换前启动的运行只读旧快照：模型、置信阈值与客户端绑定都不随之改变。"""

    revision_a = _activate(harness, "t215-model-a", confidence_threshold=0.8)
    client_stub = object()
    factory = consumer_fakes.RecordingClientFactory(
        {"subjective_grading": lambda _profile: client_stub}
    )
    runtime_service = _t215_runtime(harness, factory)
    fixture = workflow_env["fixture"]
    agent = _StubAgent(str(fixture.submission_id))
    service = _t215_service(workflow_env, agent=agent, runtime_snapshots=runtime_service)

    started = asyncio.run(
        service.start_run(
            submission_id=str(fixture.submission_id),
            actor_id=str(fixture.teacher_id),
            request_id="request-t215-switch",
        )
    )
    assert started.status is WorkflowStatus.PAUSED

    with Session(workflow_env["engine"]) as session:
        run = session.scalars(select(WorkflowRun)).one()
    state = service.checkpoints.restore_state(run)
    snapshot_id = state["snapshot_id"]
    assert isinstance(snapshot_id, str)
    # 客观题与主观题都在同一快照边界内完成评分；不存在第二个模型来源。
    assert [answer for answer, _ in agent.calls] == [
        str(fixture.objective_answer_id),
        str(fixture.subjective_answer_id),
    ]
    runtimes = [runtime for _, runtime in agent.calls]
    assert all(runtime is not None for runtime in runtimes)
    assert {runtime.snapshot_id for runtime in runtimes} == {UUID(str(snapshot_id))}
    assert {runtime.requested_model("subjective_grading") for runtime in runtimes} == {
        "t215-model-a"
    }
    saved = t211.facts_text(runtime_service.load(snapshot_id))

    _activate(
        harness,
        "t215-model-b",
        expected=revision_a.revision_id,
        confidence_threshold=0.99,
    )

    workflow = service.workflow_for_run(run, state)
    assert isinstance(workflow, GradingWorkflow)
    resumed = workflow.deps.runtime
    assert resumed is not None
    assert resumed.snapshot_id == UUID(str(snapshot_id))
    assert resumed.requested_model("subjective_grading") == "t215-model-a"
    assert resumed.confidence_threshold == pytest.approx(0.8)
    assert t211.facts_text(resumed.snapshot) == saved
    assert "t215-model-b" not in saved

    lease = resumed.acquire("subjective_grading")
    try:
        task_kind, model_id, client = factory.created[-1]
        assert (task_kind, model_id) == ("subjective_grading", "t215-model-a")
        assert client is client_stub and lease.client is client_stub
    finally:
        lease.release()
    assert lease.released is True
    assert runtime_service.active_lease_count == 0


def test_pending_review_run_never_finalizes_before_or_after_switch(
    harness: t211.SettingsHarness, workflow_env: dict[str, Any]
) -> None:
    """待复核运行不产生最终整卷结果，切到新配置后也不能直接最终化。"""

    revision_a = _activate(harness, "t215-model-a")
    fixture = workflow_env["fixture"]
    agent = _StubAgent(str(fixture.submission_id))
    service = _t215_service(
        workflow_env,
        agent=agent,
        runtime_snapshots=_t215_runtime(
            harness, consumer_fakes.RecordingClientFactory({})
        ),
    )
    started = asyncio.run(
        service.start_run(
            submission_id=str(fixture.submission_id),
            actor_id=str(fixture.teacher_id),
            request_id="request-t215-pending",
        )
    )
    assert started.status is WorkflowStatus.PAUSED
    # 桩 Agent 对两题都返回待复核；运行级要求是“不得因此产生最终成绩”。
    assert set(started.pending_answer_ids) == {
        str(fixture.objective_answer_id),
        str(fixture.subjective_answer_id),
    }

    with Session(workflow_env["engine"]) as session:
        run = session.scalars(select(WorkflowRun)).one()
    state = service.checkpoints.restore_state(run)
    assert state.get("exam_result") is None
    assert set(pending_review_answer_ids(state)) == {
        str(fixture.objective_answer_id),
        str(fixture.subjective_answer_id),
    }
    graded = state["grading_results"]
    assert graded
    assert {item.review_status for item in graded.values()} == {
        ReviewStatus.PENDING_REVIEW.value
    }

    _activate(
        harness,
        "t215-model-b",
        expected=revision_a.revision_id,
        confidence_threshold=0.99,
    )

    teacher_view = service.get_run(
        workflow_id=run.workflow_id,
        actor_id=str(fixture.teacher_id),
        roles=(UserRole.TEACHER,),
    )
    assert teacher_view.status is WorkflowStatus.PAUSED
    assert set(teacher_view.pending_answer_ids) == {
        str(fixture.objective_answer_id),
        str(fixture.subjective_answer_id),
    }

    with Session(workflow_env["engine"]) as session:
        refreshed = session.scalars(select(WorkflowRun)).one()
    after = service.checkpoints.restore_state(refreshed)
    assert after.get("exam_result") is None
    assert after["snapshot_id"] == state["snapshot_id"]
    assert set(pending_review_answer_ids(after)) == {
        str(fixture.objective_answer_id),
        str(fixture.subjective_answer_id),
    }
    assert {item.review_status for item in after["grading_results"].values()} == {
        ReviewStatus.PENDING_REVIEW.value
    }


def test_legacy_run_without_snapshot_stays_unknown_after_activation(
    harness: t211.SettingsHarness, workflow_env: dict[str, Any]
) -> None:
    """无快照的历史运行保持 legacy_unknown：运行时不换用新配置，也不伪造快照。"""

    revision_a = _activate(harness, "t215-model-a")
    fixture = workflow_env["fixture"]
    agent = _StubAgent(str(fixture.submission_id))
    service = _t215_service(workflow_env, agent=agent, runtime_snapshots=None)

    started = asyncio.run(
        service.start_run(
            submission_id=str(fixture.submission_id),
            actor_id=str(fixture.teacher_id),
            request_id="request-t215-legacy",
        )
    )
    assert started.status is WorkflowStatus.PAUSED
    assert agent.calls
    assert all(runtime is None for _, runtime in agent.calls)

    with Session(workflow_env["engine"]) as session:
        run = session.scalars(select(WorkflowRun)).one()
    state = service.checkpoints.restore_state(run)
    assert "snapshot_id" not in state
    assert t211.snapshot_files(harness.root) == {}

    resolver = _t215_runtime(harness, consumer_fakes.RecordingClientFactory({}))
    resolved = resolver.resolve_for_state(state)
    assert resolved["origin"] == t211.LEGACY_UNKNOWN
    assert resolved["snapshot"] is None

    _activate(
        harness,
        "t215-model-b",
        expected=revision_a.revision_id,
        confidence_threshold=0.99,
    )

    workflow = service.workflow_for_run(run, state)
    assert workflow.deps.runtime is None
    bindings = snapshot_bindings_from_rows(
        [(run.workflow_id, run.status, t211.legacy_payload(state))]
    )
    projected = legacy_run_index(lambda: bindings)()
    assert [fact["workflow_id"] for fact in projected] == [run.workflow_id]
    assert projected[0]["snapshot_id"] is None
