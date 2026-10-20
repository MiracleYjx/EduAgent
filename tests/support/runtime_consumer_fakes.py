"""T213 Fake/Mock consumer seams for runtime snapshots.

Only external AI transports are replaced. Snapshots, bindings, policy lookup,
blocking and release ownership remain product decisions; these helpers only
record what the product actually asked for.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from backend.app.ai.agents.state import AgentInput, AgentType, QuestionGenerationRequest
from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.retrieval.base import RetrievedChunk
from backend.app.ai.vision.base import VisionObservation, VisionResult
from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import QuestionCandidate
from backend.app.schemas.settings import ConfigurationValues, SettingsRevision
from backend.app.services.grading.confidence_policy import ConfidencePolicy
from backend.app.services.grading.grading_context import SubjectiveGradingSource
from backend.app.services.settings.runtime_snapshot import (
    RunSnapshotContext,
    RuntimeSnapshotService,
)
from tests.support import runtime_settings_fakes as settings_fakes


class FakeLLM:
    """A recording structured-output transport; it never reads configuration."""

    provider_name = "fake"

    def __init__(
        self,
        model: str,
        *,
        payload: Any = None,
        factory: Callable[[type[Any]], Any] | None = None,
        returned_model: str | None = None,
        error: BaseException | None = None,
        vision: bool = False,
    ) -> None:
        self.model_name = model
        self.last_response_model = returned_model
        self.payload = payload
        self.factory = factory
        self.error = error
        self.vision = vision
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.close_calls = 0

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[Any],
        model: str | None = None,
    ) -> Any:
        self.calls.append({"messages": list(messages), "schema": schema, "model": model})
        if self.error is not None:
            raise self.error
        if self.factory is not None:
            return self.factory(schema)
        return self.payload

    def describe(self, *, prompt_version: str | None = None) -> dict[str, str]:
        return {
            "provider": "fake",
            "model": self.model_name,
            "prompt_version": prompt_version or "unknown",
            "call_path": "fake.FakeLLM.generate_structured",
        }

    def supports_vision(self) -> bool:
        return self.vision

    async def aclose(self) -> None:
        self.closed = True
        self.close_calls += 1


class FakeEmbedding(BaseEmbeddingProvider):
    """合成 Embedding：继承产品抽象契约，从而提供 T214 要求的索引身份描述。"""

    provider_name = "fake"

    def __init__(self, model: str) -> None:
        self.model_name = model
        self.queries: list[str] = []
        self.closed = False

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.1, 0.2, 0.3]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def aclose(self) -> None:
        self.closed = True


class FakeReranker:
    provider_name = "fake"

    def __init__(self, model: str) -> None:
        self.model_name = model
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    async def rerank_async(
        self,
        query: str,
        candidates: Sequence[Any],
        top_k: int,
    ) -> list[Any]:
        self.calls.append((query, top_k))
        return list(candidates)[:top_k]

    async def aclose(self) -> None:
        self.closed = True


class StaticRetriever:
    """Return fixed chunks without a database; records the actual query."""

    def __init__(self, chunks: Sequence[RetrievedChunk]) -> None:
        self.chunks = list(chunks)
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        session: Any,
        query: Any,
        *,
        top_k: int = 5,
        filters: Any = None,
    ) -> list[RetrievedChunk]:
        del session
        self.calls.append({"query": query, "top_k": top_k, "filters": filters})
        return list(self.chunks)


def make_chunk(chunk_id: str = "chunk-1") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        course_id="course-t213",
        document_id="document-t213",
        content="变量用于保存数据，并可在后续语句中引用。",
        rank=1,
        semantic_score=0.9,
    )


class RecordingClientFactory:
    """Build fake clients from the profile selected by the product snapshot."""

    def __init__(self, builders: Mapping[str, Callable[[dict[str, Any]], Any]]) -> None:
        self._builders = dict(builders)
        self.created: list[tuple[str, str | None, Any]] = []

    def __call__(self, task_kind: str, profile: Mapping[str, Any]) -> Any:
        builder = self._builders.get(task_kind)
        if builder is None:
            raise AssertionError(f"unexpected snapshot client request: {task_kind}")
        client = builder(dict(profile))
        self.created.append((task_kind, profile.get("model_id"), client))
        return client


def add_profile(
    values: dict[str, Any],
    *,
    adapter_id: str,
    model_id: str,
    capabilities: Sequence[str],
    base_url: str = "https://model.invalid/v1",
    auth_mode: str = "none",
    parameters: Mapping[str, Any] | None = None,
    resource_directory: Path | None = None,
    credential_ref: Mapping[str, Any] | None = None,
) -> tuple[UUID, UUID]:
    """Add one non-secret provider profile to a harness value document."""

    profile_id, revision = uuid4(), uuid4()
    values["providers"][str(profile_id)] = {
        "profile_id": str(profile_id),
        "profile_revision": str(revision),
        "display_name": f"T213 {model_id}",
        "adapter_id": adapter_id,
        "model_id": model_id,
        "base_url": base_url,
        "auth_mode": auth_mode,
        "declared_capabilities": list(capabilities),
        "enabled": True,
        **({"parameters": dict(parameters)} if parameters else {}),
        **(
            {"resource_directory": str(resource_directory)}
            if resource_directory is not None
            else {}
        ),
        **({"credential_ref": dict(credential_ref)} if credential_ref else {}),
    }
    return profile_id, revision


def bind_task(
    values: dict[str, Any],
    task_kind: str,
    profile_id: UUID,
    revision: UUID,
    *,
    retrieval_mode: str | None = None,
) -> None:
    values["bindings"][task_kind] = {
        "task_kind": task_kind,
        "profile_id": str(profile_id),
        "profile_revision": str(revision),
        "required_capabilities": [],
        **({"retrieval_mode": retrieval_mode} if retrieval_mode is not None else {}),
    }


def add_embedding_and_rerank(
    values: dict[str, Any],
    *,
    embedding_model: str = "t213-embedding-a",
    rerank_model: str = "t213-rerank-a",
) -> tuple[UUID, UUID, UUID, UUID]:
    """Add independent Embedding and LLM-rerank profiles plus task bindings."""

    embedding_id, embedding_revision = add_profile(
        values,
        adapter_id="openai_compatible",
        model_id=embedding_model,
        capabilities=("embedding",),
        base_url="https://embedding.invalid/v1",
        parameters={"dimension": 1024, "batch_size": 8},
    )
    bind_task(values, "embedding", embedding_id, embedding_revision)
    rerank_id, rerank_revision = add_profile(
        values,
        adapter_id="openai_compatible",
        model_id=rerank_model,
        capabilities=("llm_rerank",),
        base_url="https://rerank.invalid/v1",
        parameters={"max_candidates": 5, "timeout_seconds": 5.0},
    )
    bind_task(values, "rerank", rerank_id, rerank_revision)
    return embedding_id, embedding_revision, rerank_id, rerank_revision


def add_vision(values: dict[str, Any], *, model: str = "t213-vision-a") -> tuple[UUID, UUID]:
    """Add a DeepSeek vision profile; the fake factory replaces its transport."""

    vision_id, revision = add_profile(
        values,
        adapter_id="deepseek",
        model_id=model,
        capabilities=("vision", "text", "json_object"),
        base_url="https://vision.invalid/v1",
        auth_mode="api_key",
    )
    bind_task(values, "vision", vision_id, revision)
    return vision_id, revision


class SyntheticConfig:
    """A structurally valid revision source for multi-task snapshots.

    T210 activation of Embedding/Rerank/Vision is outside T213. These tests still use the
    real RuntimeSnapshotService, real Registry validation and the real credential store;
    only the already-validated revision document is supplied directly.
    """

    def __init__(self, base: settings_fakes.SettingsHarness, revision: SettingsRevision) -> None:
        self._base = base.service
        self._revision = revision
        self.writable = True

    @property
    def config(self) -> Path:
        return self._base.config

    @property
    def root(self) -> Path:
        return self._base.root

    @property
    def environment_id(self) -> UUID:
        return self._base.environment_id

    @property
    def provider_registry(self) -> Any:
        return self._base.provider_registry

    @property
    def credential_store(self) -> Any:
        return self._base.credential_store

    def reference_transaction(self) -> Any:
        return nullcontext()

    def active_revision(self) -> SettingsRevision:
        return self._revision

    def set_revision(self, revision: SettingsRevision) -> None:
        self._revision = revision


def synthetic_revision(
    harness: settings_fakes.SettingsHarness,
    mutate: Callable[[dict[str, Any]], None],
) -> SettingsRevision:
    """Build a validated revision document without invoking T210 activation."""

    base = harness.service.active_revision()
    if base is None:
        raise AssertionError("synthetic revision requires one activated baseline")
    data = base.values.model_dump(mode="python")
    mutate(data)
    values = ConfigurationValues.model_validate(
        data, context={"provider_registry": harness.admission.registry}
    )
    return base.model_copy(
        update={
            "revision_id": uuid4(),
            "parent_revision": base.revision_id,
            "values": values,
            "status": "active",
            "activated_at": datetime.now(UTC),
        }
    )


def snapshot_context(
    harness_or_config: settings_fakes.SettingsHarness | SyntheticConfig,
    factory: RecordingClientFactory,
    *,
    task_kinds: Sequence[str] = ("subjective_grading",),
    optional_task_kinds: Sequence[str] = (),
    workflow_id: str = "workflow-t213",
    storage_root: Path | None = None,
) -> RunSnapshotContext:
    """Create one persisted snapshot and return its consumer context."""

    config: Any = getattr(harness_or_config, "service", harness_or_config)
    service = RuntimeSnapshotService(config, client_factory=factory)
    snapshot = service.create(
        workflow_id=workflow_id,
        request_id=f"request-{workflow_id}",
        task_kinds=task_kinds,
        optional_task_kinds=optional_task_kinds,
        storage_root=storage_root,
    )
    return service.context(snapshot)


def activate_model(
    harness: settings_fakes.SettingsHarness,
    model_id: str,
    *,
    expected: UUID | None = None,
) -> Any:
    return asyncio.run(harness.activate_model(model_id, expected=expected))


def sample_question_input(course_id: UUID | None = None) -> AgentInput:
    return AgentInput(
        agent_type=AgentType.QUESTION,
        request_id="request-t213-question",
        generation_request=QuestionGenerationRequest(
            course_id=str(course_id or uuid4()),
            knowledge_points=["函数极限"],
            difficulty="中等",
            count=1,
        ),
    )


def question_payload(*, source_context_id: str = "chunk-1") -> Any:
    from backend.app.ai.agents.question_agent import QuestionGenerationPayload

    return QuestionGenerationPayload(
        candidates=[
            QuestionCandidate(
                question_type=QuestionType.SHORT_ANSWER,
                content="说明函数极限的定义。",
                reference_answer="描述趋近过程。",
                scoring_rubric="定义完整得满分。",
                difficulty="中等",
                knowledge_points=["函数极限"],
                score=10.0,
                source_context_ids=[source_context_id],
            )
        ]
    )


def sample_subjective_source() -> SubjectiveGradingSource:
    return SubjectiveGradingSource(
        question_type=QuestionType.SHORT_ANSWER,
        course_id=str(uuid4()),
        question_content="说明变量赋值的作用。",
        reference_answer="保存数据并可供后续引用。",
        student_answer="变量用于保存数据。",
        scoring_rubric="说明保存与引用得满分。",
        knowledge_points=("变量",),
        question_id=str(uuid4()),
        answer_id=str(uuid4()),
        submission_id=str(uuid4()),
    )


def scoring_payload(*, score: float = 6.0, confidence: float = 0.9) -> Any:
    from backend.app.services.grading.subjective_grader import SubjectiveGradingPayload

    return SubjectiveGradingPayload(
        score=Decimal(str(score)),
        confidence=confidence,
        reason="说明了保存作用。",
        correct_points=["保存数据"],
        missing_knowledge_points=["引用数据"],
        suggestions=["补充引用。"],
    )


def diagnosis_payload() -> Any:
    from backend.app.services.diagnosis_service import LearningSuggestions

    return LearningSuggestions(suggestions=["复习变量引用。"])


def semantic_payload() -> Any:
    from backend.app.schemas.content_validation import (
        SemanticMachineOutput,
        ValidationCheck,
    )

    kinds = (
        "answer_correctness",
        "condition_sufficiency",
        "option_ambiguity",
        "rubric_clarity",
    )
    return SemanticMachineOutput(
        checks=[
            ValidationCheck(
                kind=cast(Any, kind), verdict="pass", reason="合成输入通过。"
            )
            for kind in kinds
        ],
        issues=[],
    )


def vision_payload() -> VisionResult:
    return VisionResult(
        observations=[
            VisionObservation(
                image_index=1,
                kind="condition",
                description="图中标明变量赋值。",
                bbox=(0, 0, 8, 6),
            )
        ],
        conditions=[],
        unresolved_issues=[],
        requires_manual_review=False,
    )


def tiny_png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    stream = BytesIO()
    Image.new("RGB", (8, 6), "white").save(stream, format="PNG")
    return stream.getvalue()


@dataclass
class FakeImagePersistence:
    """Minimum persistence projection used by ImageUnderstandingService."""

    image: Any
    finished: dict[str, Any] | None = field(default=None)

    def start_image_run(self, *_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(id=uuid4())

    def prepare_image_run(self, *_args: Any, **_kwargs: Any) -> Any:
        from backend.app.services.content_validation_service import PreparedImageRun

        run = SimpleNamespace(id=uuid4())
        return PreparedImageRun(
            run=cast(Any, run), context={"task": "synthetic"}, images=(self.image,)
        )

    def finish_image_run(self, *_args: Any, **kwargs: Any) -> Any:
        self.finished = dict(kwargs)
        return SimpleNamespace(id=uuid4())


def prepared_image() -> Any:
    from backend.app.services.content_validation_service import PreparedImage

    return PreparedImage(
        asset_id=uuid4(),
        file_id="file-t213",
        image_index=1,
        data=tiny_png_bytes(),
        mime_type="image/png",
        width=8,
        height=6,
        source_page_id=None,
        region=None,
    )


def policy(threshold: float = 0.8) -> ConfidencePolicy:
    return ConfidencePolicy(threshold=threshold)


__all__ = [
    "FakeEmbedding",
    "FakeImagePersistence",
    "FakeLLM",
    "FakeReranker",
    "RecordingClientFactory",
    "StaticRetriever",
    "SyntheticConfig",
    "activate_model",
    "add_embedding_and_rerank",
    "add_profile",
    "add_vision",
    "bind_task",
    "diagnosis_payload",
    "make_chunk",
    "policy",
    "prepared_image",
    "question_payload",
    "sample_question_input",
    "sample_subjective_source",
    "scoring_payload",
    "semantic_payload",
    "snapshot_context",
    "synthetic_revision",
    "tiny_png_bytes",
    "vision_payload",
]
