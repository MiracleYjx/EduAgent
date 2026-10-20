"""TCR-S5-RUNTIME-SNAPSHOT-CONSUMERS-015：T213 真实消费者链的 Fake/Mock 集成。

只替换外部 Provider 传输与持久化投影；快照创建、绑定解析、策略/凭据选择、
阻断与释放所有权仍由产品代码决定。所有目录/配置/数据库均为本测试进程的合成资源。
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backend.app.ai.agents.invocation import AgentInvocation
from backend.app.ai.agents.question_agent import QuestionAgent
from backend.app.ai.agents.state import AgentInput, AgentOutput, AgentStatus, AgentType
from backend.app.ai.retrieval.base import RetrievalMode
from backend.app.ai.workflows.grading_workflow import GradingWorkflow
from backend.app.api.workflow import WorkflowNotReadyError, WorkflowService
from backend.app.core.database import Base
from backend.app.domain.enums import (
    ReviewStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import WorkflowRun
from backend.app.schemas.ai import GradingResult as GradingResultPayload
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
)
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.diagnosis_service import DiagnosisService
from backend.app.services.grading.grading_context import build_grading_context
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.grading.subjective_grader import (
    ProviderFailedError,
    SubjectiveGrader,
)
from backend.app.services.image_understanding_service import ImageUnderstandingService
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
from backend.app.services.workflow_checkpoint import WorkflowCheckpointStore
from tests.support import runtime_consumer_fakes as fakes
from tests.support import runtime_settings_fakes as settings_fakes
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.unit.models.sqlite_support import seed_submission
from tests.unit.settings_helpers import build_test_settings


@contextmanager
def _no_settings_reads(*modules: Any) -> Generator[None, None, None]:
    """Fail if a runtime-bound consumer falls back to current AppSettings."""

    originals = [(module, module.get_settings) for module in modules]

    def forbidden() -> Any:
        raise AssertionError("runtime-bound consumer must not read current settings")

    try:
        for module, _ in originals:
            module.get_settings = forbidden
        yield
    finally:
        for module, original in originals:
            module.get_settings = original


@pytest.fixture
def harness(tmp_path: Path) -> Generator[settings_fakes.SettingsHarness, None, None]:
    built = settings_fakes.settings_harness(tmp_path / "settings")
    try:
        yield built
    finally:
        built.close()


def _revision(harness: settings_fakes.SettingsHarness, model: str = "t213-model-a") -> Any:
    return fakes.activate_model(harness, model)


def _factory(**builders: Any) -> fakes.RecordingClientFactory:
    return fakes.RecordingClientFactory(builders)


def _synthetic(
    harness: settings_fakes.SettingsHarness,
    mutate: Any,
) -> fakes.SyntheticConfig:
    return fakes.SyntheticConfig(harness, fakes.synthetic_revision(harness, mutate))


def _bind_default_subjective(runtime_mode: str) -> Any:
    def mutate(values: dict[str, Any]) -> None:
        default = values["bindings"]["default_llm"]
        fakes.bind_task(
            values,
            "subjective_grading",
            default["profile_id"],
            default["profile_revision"],
            retrieval_mode=runtime_mode,
        )

    return mutate


def test_snapshot_client_uses_saved_profile_after_activation_switch(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    revision = _revision(harness)
    factory = _factory(subjective_grading=lambda profile: fakes.FakeLLM(profile["model_id"]))
    runtime = fakes.snapshot_context(
        harness,
        factory,
        task_kinds=("subjective_grading",),
        storage_root=tmp_path / "storage",
    )
    fakes.activate_model(harness, "t213-model-b", expected=revision.revision_id)

    lease = runtime.acquire("subjective_grading")
    try:
        assert lease.client.model_name == "t213-model-a"
        assert lease.client.closed is False
    finally:
        asyncio.run(lease.arelease())

    assert factory.created[0][0] == "subjective_grading"
    assert factory.created[0][1] == "t213-model-a"
    assert runtime.service.active_lease_count == 0


def test_question_agent_uses_snapshot_after_switch_and_records_source(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    revision = _revision(harness)
    provider = fakes.FakeLLM(
        "t213-model-a",
        payload=fakes.question_payload(),
        returned_model="provider-echo-a",
    )
    factory = _factory(question_generation=lambda _profile: provider)
    runtime = fakes.snapshot_context(
        harness,
        factory,
        task_kinds=("question_generation",),
        storage_root=tmp_path / "storage",
    )
    fakes.activate_model(harness, "t213-model-b", expected=revision.revision_id)

    retriever = fakes.StaticRetriever([fakes.make_chunk()])
    import backend.app.ai.agents.question_agent as question_module

    with _no_settings_reads(question_module):
        output = asyncio.run(
            QuestionAgent().generate(
                cast(Any, object()),
                fakes.sample_question_input(),
                retriever=cast(Any, retriever),
                mode=RetrievalMode.KEYWORD_ONLY,
                runtime=runtime,
            )
        )

    assert output.status is AgentStatus.SUCCESS
    assert output.model == "t213-model-a"
    assert output.prompt_version == "question-generation-v2"
    assert len(provider.calls) == 1
    invocation = runtime.last_invocation("question_generation")
    assert invocation is not None
    assert invocation.requested_model == "t213-model-a"
    assert invocation.returned_model == "provider-echo-a"
    assert invocation.prompt_version == "question-generation-v2"
    assert invocation.prompt_source == "consumer"
    assert provider.closed is True
    assert runtime.service.active_lease_count == 0


def test_new_snapshot_uses_new_active_model(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    revision = _revision(harness)
    fakes.activate_model(harness, "t213-model-b", expected=revision.revision_id)
    factory = _factory(
        question_generation=lambda profile: fakes.FakeLLM(
            profile["model_id"], payload=fakes.question_payload()
        )
    )
    runtime = fakes.snapshot_context(
        harness,
        factory,
        task_kinds=("question_generation",),
        workflow_id="workflow-t213-new",
        storage_root=tmp_path / "storage",
    )
    import backend.app.ai.agents.question_agent as question_module

    with _no_settings_reads(question_module):
        output = asyncio.run(
            QuestionAgent().generate(
                cast(Any, object()),
                fakes.sample_question_input(),
                retriever=cast(Any, fakes.StaticRetriever([fakes.make_chunk()])),
                mode=RetrievalMode.KEYWORD_ONLY,
                runtime=runtime,
            )
        )
    assert output.model == "t213-model-b"


def test_subjective_grader_uses_snapshot_and_releases_client(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    _revision(harness)
    config = _synthetic(harness, _bind_default_subjective("keyword"))
    provider = fakes.FakeLLM(
        "t213-model-a",
        payload=fakes.scoring_payload(),
        returned_model="provider-grading-a",
    )
    factory = _factory(subjective_grading=lambda _profile: provider)
    runtime = fakes.snapshot_context(
        config,
        factory,
        task_kinds=("subjective_grading",),
        storage_root=tmp_path / "storage",
    )

    import backend.app.services.grading.grading_context as context_module
    import backend.app.services.grading.subjective_grader as grader_module

    with _no_settings_reads(context_module, grader_module):
        result = asyncio.run(
            SubjectiveGrader().grade(
                cast(Any, object()),
                fakes.sample_subjective_source(),
                max_score=10,
                retriever=cast(Any, fakes.StaticRetriever([fakes.make_chunk()])),
                runtime=runtime,
            )
        )

    assert result.score == Decimal("6.00")
    assert result.review_status == ReviewStatus.NOT_REQUIRED.value
    invocation = runtime.last_invocation("subjective_grading")
    assert invocation is not None
    assert invocation.requested_model == "t213-model-a"
    assert invocation.returned_model == "provider-grading-a"
    assert invocation.prompt_version == "subjective-grading-v1"
    assert provider.closed is True
    assert runtime.service.active_lease_count == 0


def test_subjective_grader_releases_client_on_provider_failure(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    _revision(harness)
    config = _synthetic(harness, _bind_default_subjective("keyword"))
    provider = fakes.FakeLLM(
        "t213-model-a",
        error=RuntimeError("synthetic provider failure"),
    )
    factory = _factory(subjective_grading=lambda _profile: provider)
    runtime = fakes.snapshot_context(
        config,
        factory,
        task_kinds=("subjective_grading",),
        storage_root=tmp_path / "storage",
    )

    import backend.app.services.grading.grading_context as context_module
    import backend.app.services.grading.subjective_grader as grader_module

    with _no_settings_reads(context_module, grader_module), pytest.raises(
        ProviderFailedError
    ):
        asyncio.run(
            SubjectiveGrader().grade(
                cast(Any, object()),
                fakes.sample_subjective_source(),
                max_score=10,
                retriever=cast(Any, fakes.StaticRetriever([fakes.make_chunk()])),
                runtime=runtime,
            )
        )

    invocation = runtime.last_invocation("subjective_grading")
    assert invocation is not None
    assert invocation.requested_model == "t213-model-a"
    assert invocation.returned_model is None
    assert provider.closed is True
    assert runtime.service.active_lease_count == 0


def test_grading_context_uses_snapshot_embedding_and_rerank(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    _revision(harness)

    def mutate(values: dict[str, Any]) -> None:
        _bind_default_subjective("hybrid_rerank")(values)
        fakes.add_embedding_and_rerank(values)

    config = _synthetic(harness, mutate)
    embedding = fakes.FakeEmbedding("t213-embedding-a")
    reranker = fakes.FakeReranker("t213-rerank-a")
    factory = _factory(
        embedding=lambda _profile: embedding,
        rerank=lambda _profile: reranker,
        subjective_grading=lambda _profile: fakes.FakeLLM(
            "t213-model-a", payload=fakes.scoring_payload()
        ),
    )
    runtime = fakes.snapshot_context(
        config,
        factory,
        task_kinds=("subjective_grading", "embedding", "rerank"),
        storage_root=tmp_path / "storage",
    )

    import backend.app.services.grading.grading_context as context_module

    with _no_settings_reads(context_module):
        context = asyncio.run(
            build_grading_context(
                cast(Any, object()),
                fakes.sample_subjective_source(),
                retriever=cast(Any, fakes.StaticRetriever([fakes.make_chunk()])),
                runtime=runtime,
            )
        )

    assert runtime.retrieval_mode("subjective_grading") == "hybrid_rerank"
    assert embedding.queries
    assert reranker.calls
    assert context.retrieved_context_ids == ("chunk-1",)
    assert embedding.closed is True
    assert reranker.closed is True
    assert runtime.service.active_lease_count == 0


def test_build_reranker_uses_snapshot_binding(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    _revision(harness)

    def mutate(values: dict[str, Any]) -> None:
        default = values["bindings"]["default_llm"]
        fakes.bind_task(
            values,
            "subjective_grading",
            default["profile_id"],
            default["profile_revision"],
            retrieval_mode="hybrid_rerank",
        )
        fakes.add_embedding_and_rerank(values)

    config = _synthetic(harness, mutate)
    reranker = fakes.FakeReranker("t213-rerank-a")
    runtime = fakes.snapshot_context(
        config,
        _factory(rerank=lambda _profile: reranker),
        task_kinds=("subjective_grading", "embedding", "rerank"),
        storage_root=tmp_path / "storage",
    )

    from backend.app.ai.retrieval.reranker import build_reranker

    assert build_reranker(runtime=runtime) is reranker
    asyncio.run(runtime.aclose())
    assert reranker.closed is True
    assert runtime.service.active_lease_count == 0


def test_diagnosis_uses_snapshot_and_keeps_unknown_returned_model(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    revision = _revision(harness)
    provider = fakes.FakeLLM("t213-model-a", payload=fakes.diagnosis_payload())
    factory = _factory(diagnosis=lambda _profile: provider)
    runtime = fakes.snapshot_context(
        harness,
        factory,
        task_kinds=("diagnosis",),
        storage_root=tmp_path / "storage",
    )
    fakes.activate_model(harness, "t213-model-b", expected=revision.revision_id)
    exam_result = settings_fakes.business_items()[1]

    report = asyncio.run(DiagnosisService().generate(exam_result, runtime=runtime))

    assert report.status is DiagnosisStatus.READY
    assert report.learning_suggestions == ["复习变量引用。"]
    invocation = runtime.last_invocation("diagnosis")
    assert invocation is not None
    assert invocation.requested_model == "t213-model-a"
    assert invocation.returned_model is None
    assert invocation.prompt_version == "unversioned"
    assert invocation.prompt_source == "unversioned"
    assert provider.closed is True
    assert runtime.service.active_lease_count == 0


def test_image_understanding_uses_snapshot_vision_and_releases(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    _revision(harness)
    config = _synthetic(harness, lambda values: fakes.add_vision(values))
    provider = fakes.FakeLLM(
        "t213-vision-a",
        payload=fakes.vision_payload(),
        returned_model="provider-vision-a",
        vision=True,
    )
    factory = _factory(vision=lambda _profile: provider)
    runtime = fakes.snapshot_context(
        config,
        factory,
        task_kinds=("vision",),
        storage_root=tmp_path / "storage",
    )
    persistence = fakes.FakeImagePersistence(fakes.prepared_image())
    service = ImageUnderstandingService(cast(Any, object()), runtime=runtime)
    service.persistence = persistence  # type: ignore[assignment]

    asyncio.run(service.understand("question", uuid4(), actor_id=uuid4()))

    assert len(provider.calls) == 1
    invocation = runtime.last_invocation("vision")
    assert invocation is not None
    assert invocation.requested_model == "t213-vision-a"
    assert invocation.returned_model == "provider-vision-a"
    assert invocation.prompt_version == "vision-conditions-v1"
    assert persistence.finished is not None
    assert provider.closed is True
    assert runtime.service.active_lease_count == 0


def test_content_validation_uses_snapshot_provider_and_records_prompt(
    harness: settings_fakes.SettingsHarness, tmp_path: Path
) -> None:
    _revision(harness)
    provider = fakes.FakeLLM(
        "t213-model-a",
        factory=lambda schema: fakes.semantic_payload(),
    )
    factory = _factory(content_validation=lambda _profile: provider)
    runtime = fakes.snapshot_context(
        harness,
        factory,
        task_kinds=("content_validation",),
        storage_root=tmp_path / "storage",
    )
    service = ContentValidationService(cast(Any, object()), runtime=runtime)
    report_id = uuid4()
    service.prepare_validation = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[assignment]
        report=SimpleNamespace(id=report_id),
        input=SimpleNamespace(model_dump_json=lambda: "{}"),
    )
    service.finish_validation = lambda *_args, **_kwargs: SimpleNamespace(id=report_id)  # type: ignore[assignment]

    asyncio.run(service.run_validation(uuid4(), actor_id=uuid4(), runtime=runtime))

    assert len(provider.calls) == 1
    invocation = runtime.last_invocation("content_validation")
    assert invocation is not None
    assert invocation.requested_model == "t213-model-a"
    assert invocation.returned_model is None
    assert invocation.prompt_version == "question-semantics-v2"
    assert invocation.prompt_source == "consumer"
    assert provider.closed is True
    assert runtime.service.active_lease_count == 0


# ---------------------------------------------------------------------------
# Workflow start/resume: snapshot persistence precedes the first Agent call.
# ---------------------------------------------------------------------------


def _file_engine(path: Path) -> Any:
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def workflow_env(tmp_path: Path) -> Generator[dict[str, Any], None, None]:
    engine = _file_engine(tmp_path / "workflow.db")
    with Session(engine) as session:
        fixture = seed_submission(session)
        confirm_synthetic_exam_basis(session, fixture.exam_id)
        session.commit()
        yield {"engine": engine, "fixture": fixture}
    engine.dispose()


class _StubAgent:
    """Pauses the first subjective answer; records the runtime it actually got."""

    def __init__(self, submission_id: str, on_call: Any = None) -> None:
        self.submission_id = submission_id
        self.on_call = on_call
        self.calls: list[tuple[str, Any]] = []

    async def grade_answer_async(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        request_id: str,
        workflow_id: str | None = None,
        session: Any = None,
        settings: Any = None,
        runtime: Any = None,
    ) -> AgentInvocation:
        del session, settings
        self.calls.append((target.answer_id, runtime))
        if self.on_call is not None:
            self.on_call(runtime)
        assert target.max_score is not None
        result = GradingResultPayload(
            question_type=target.question_type,
            score=Decimal("6.00"),
            max_score=target.max_score,
            reason="说明了变量的作用。",
            correct_points=["保存数据"],
            missing_knowledge_points=["引用数据"],
            knowledge_points=list(target.knowledge_points),
            suggestions=["补充变量引用。"],
            confidence=0.3,
            validation_status=ValidationStatus.VALIDATED.value,
            review_status=ReviewStatus.PENDING_REVIEW.value,
            answer_id=target.answer_id,
            submission_id=self.submission_id,
            exam_question_id=(
                target.scoring_input.exam_question_id if target.scoring_input else None
            ),
        )
        decision = ConfidenceDecisionDTO(
            confidence=0.3,
            threshold=0.8,
            requires_review=True,
            review_status=ReviewStatus.PENDING_REVIEW.value,
            grading_status="Pending Review",
            reason="置信度低于阈值，已进入人工复核队列。",
        )
        return AgentInvocation(
            request_id=request_id,
            workflow_id=workflow_id,
            input=AgentInput(
                agent_type=AgentType.GRADING,
                request_id=request_id,
                workflow_id=workflow_id,
                submission_context=snapshot.to_context(),
                answer_id=target.answer_id,
                question_id=target.question_id,
                question_type=target.question_type,
                scoring_input=target.scoring_input,
            ),
            output=AgentOutput(
                agent_type=AgentType.GRADING,
                status=AgentStatus.PENDING_REVIEW,
                validation_status=ValidationStatus.VALIDATED,
                question_type=target.question_type,
                grading_result=result,
                confidence=0.3,
                confidence_decision=decision,
                requires_review=True,
            ),
        )

    def score(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("workflow must call grade_answer_async per answer")


class _StubDiagnosis:
    async def generate(self, _exam_result: Any) -> DiagnosisReportDTO:
        return DiagnosisReportDTO(
            submission_id="unused",
            student_id="unused",
            status=DiagnosisStatus.NOT_READY,
            generated_at=datetime.now(UTC),
        )


def _workflow_service(
    env: dict[str, Any],
    *,
    agent: Any,
    runtime_snapshots: RuntimeSnapshotService,
) -> WorkflowService:
    return WorkflowService(
        checkpoints=WorkflowCheckpointStore(session_factory=lambda: Session(env["engine"])),
        reader=DatabaseGradingSubmissionReader(
            session_factory=lambda: Session(env["engine"])
        ),
        session_factory=lambda: Session(env["engine"]),
        agent=agent,
        diagnosis_service=_StubDiagnosis(),
        repository=DatabaseGradingRepository(
            session_factory=lambda: Session(env["engine"])
        ),
        settings=build_test_settings(confidence_threshold=0.8),
        runtime_snapshots=runtime_snapshots,
    )


def test_workflow_persists_snapshot_before_agent_and_resume_keeps_binding(
    harness: settings_fakes.SettingsHarness,
    workflow_env: dict[str, Any],
) -> None:
    revision = _revision(harness)
    observed: list[dict[str, Any]] = []

    def on_call(runtime: Any) -> None:
        assert runtime is not None
        path = runtime.service.path_for(runtime.snapshot_id)
        observed.append(
            {
                "snapshot_id": runtime.snapshot_id,
                "exists": path.is_file(),
                "model": runtime.requested_model("subjective_grading"),
            }
        )

    agent = _StubAgent(str(workflow_env["fixture"].submission_id), on_call=on_call)
    runtime_service = RuntimeSnapshotService(
        harness.service,
        client_factory=_factory(),
    )
    service = _workflow_service(
        workflow_env, agent=agent, runtime_snapshots=runtime_service
    )

    result = asyncio.run(
        service.start_run(
            submission_id=str(workflow_env["fixture"].submission_id),
            actor_id=str(workflow_env["fixture"].teacher_id),
            request_id="request-t213-workflow",
        )
    )

    assert result.status is WorkflowStatus.PAUSED
    assert observed and observed[0]["exists"] is True
    assert observed[0]["model"] == "t213-model-a"
    with Session(workflow_env["engine"]) as session:
        run = session.scalars(select(WorkflowRun)).one()
    state = service.checkpoints.restore_state(run)
    assert state["snapshot_id"] == str(observed[0]["snapshot_id"])

    fakes.activate_model(harness, "t213-model-b", expected=revision.revision_id)
    workflow = service.workflow_for_run(run, state)
    assert isinstance(workflow, GradingWorkflow)
    assert workflow.deps.runtime is not None
    assert (
        workflow.deps.runtime.snapshot.requested_model["subjective_grading"]
        == "t213-model-a"
    )
    assert workflow.deps.runtime.snapshot_id == observed[0]["snapshot_id"]


def test_workflow_resume_blocks_when_snapshot_is_missing(
    harness: settings_fakes.SettingsHarness,
    workflow_env: dict[str, Any],
) -> None:
    _revision(harness)
    runtime_service = RuntimeSnapshotService(
        harness.service,
        client_factory=_factory(),
    )
    service = _workflow_service(
        workflow_env,
        agent=_StubAgent(str(workflow_env["fixture"].submission_id)),
        runtime_snapshots=runtime_service,
    )
    asyncio.run(
        service.start_run(
            submission_id=str(workflow_env["fixture"].submission_id),
            actor_id=str(workflow_env["fixture"].teacher_id),
            request_id="request-t213-missing",
        )
    )
    with Session(workflow_env["engine"]) as session:
        run = session.scalars(select(WorkflowRun)).one()
    state = service.checkpoints.restore_state(run)
    snapshot_id = state["snapshot_id"]
    assert snapshot_id is not None
    runtime_service.path_for(snapshot_id).unlink()

    with pytest.raises(WorkflowNotReadyError) as captured:
        service.workflow_for_run(run, state)
    assert captured.value.source_code == "SNAPSHOT_UNAVAILABLE"
