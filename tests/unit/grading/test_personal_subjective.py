"""T052 personal subjective context boundaries.

These tests use only explicit fakes; no provider or database is touched.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image

from backend.app.ai.retrieval.base import (
    BaseRetriever,
    RetrievalFilters,
    RetrievalMode,
    RetrievedChunk,
)
from backend.app.schemas.content_blocks import ImageBlock, RevisionContent
from backend.app.services.grading.grading_context import (
    GradingInputError,
    SubjectiveGradingSource,
    _merge_personal_filters,
    build_grading_context,
)
from backend.app.services.grading.scoring_images import (
    GradingImageError,
    prepare_personal_scoring_images,
)


class _ForbiddenRetriever(BaseRetriever):
    mode = RetrievalMode.KEYWORD_ONLY

    def search(self, *_args: Any, **_kwargs: Any) -> list[RetrievedChunk]:
        raise AssertionError("general knowledge must not retrieve course chunks")


class _RecordingRetriever(BaseRetriever):
    mode = RetrievalMode.KEYWORD_ONLY

    def __init__(self) -> None:
        self.filters: RetrievalFilters | None = None

    def search(
        self,
        _session: Any,
        _query: Any,
        *,
        top_k: int = 5,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        self.filters = filters
        return [
            RetrievedChunk(
                chunk_id="chunk-personal",
                course_id="personal",
                document_id=str(uuid4()),
                content="函数的定义域是输入集合。",
                subject_id=str(filters.subject_ids[0]) if filters else None,
                rank=1,
            )
        ]


def _source(**overrides: Any) -> SubjectiveGradingSource:
    values: dict[str, Any] = {
        "question_type": "short_answer",
        "subject_id": uuid4(),
        "question_content": "说明函数的定义域。",
        "reference_answer": "定义域是允许输入的集合。",
        "student_answer": "函数可以取的输入。",
        "scoring_rubric": "说明输入集合即可。",
        "grounding": "uploaded_material",
    }
    values.update(overrides)
    return SubjectiveGradingSource(**values)


def test_personal_source_requires_exactly_one_scope_identity() -> None:
    subject = uuid4()
    with pytest.raises(GradingInputError):
        _source(subject_id=subject, course_id=uuid4())
    with pytest.raises(GradingInputError):
        _source(subject_id=None, course_id=None)


def test_general_knowledge_skips_a_disabled_retrieval_binding() -> None:
    calls = []
    runtime = SimpleNamespace(retrieval_mode=lambda task: calls.append(task) or "none")
    context = asyncio.run(build_grading_context(None, _source(grounding="general_knowledge"),
        runtime=runtime, retriever=_ForbiddenRetriever()))
    assert context.retrieval_mode is None
    assert context.chunks == () and context.retrieved_context_ids == ()
    assert calls == []


def test_general_knowledge_has_no_retrieval_and_no_fabricated_chunks() -> None:
    source = _source(grounding="general_knowledge")
    context = asyncio.run(
        build_grading_context(
            None,  # type: ignore[arg-type]
            source,
            mode=RetrievalMode.KEYWORD_ONLY,
            retriever=_ForbiddenRetriever(),
        )
    )
    assert context.chunks == ()
    assert context.retrieved_context_ids == ()
    assert context.final_context == ""
    assert context.candidate_count == 0


def test_personal_uploaded_material_scope_is_narrowed_not_widened() -> None:
    subject = uuid4()
    version = uuid4()
    document = uuid4()
    from backend.app.schemas.retrieval_scope import RetrievalScope

    source = _source(
        subject_id=subject,
        material_version_ids=(str(version),),
    )
    scoped = _merge_personal_filters(
        _source(
            subject_id=subject,
            material_version_ids=(str(version),),
            retrieval_scope=RetrievalScope(document_ids=(document,)),
        ),
        None,
    )
    assert scoped.document_ids == (document,)
    retriever = _RecordingRetriever()
    context = asyncio.run(
        build_grading_context(
            None,  # type: ignore[arg-type]
            source,
            mode=RetrievalMode.KEYWORD_ONLY,
            retriever=retriever,
            require_context=False,
        )
    )
    assert retriever.filters is not None
    assert retriever.filters.subject_ids == (subject,)
    assert retriever.filters.material_version_ids == (version,)
    assert retriever.filters.document_ids == ()
    assert retriever.filters.course_ids == ()
    assert context.retrieved_context_ids == ("chunk-personal",)


def test_personal_filter_cannot_cross_subject_or_scope() -> None:
    source = _source(subject_id=uuid4(), material_version_ids=(str(uuid4()),))
    with pytest.raises(GradingInputError):
        asyncio.run(
            build_grading_context(
                None,  # type: ignore[arg-type]
                source,
                mode=RetrievalMode.KEYWORD_ONLY,
                retriever=_RecordingRetriever(),
                filters=RetrievalFilters(subject_ids=(uuid4(),)),
                require_context=False,
            )
        )
    with pytest.raises(GradingInputError):
        asyncio.run(
            build_grading_context(
                None,  # type: ignore[arg-type]
                source,
                mode=RetrievalMode.KEYWORD_ONLY,
                retriever=_RecordingRetriever(),
                filters=RetrievalFilters(material_version_ids=(uuid4(),)),
                require_context=False,
            )
        )


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_personal_images_use_only_bound_managed_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    revision_id = uuid4()
    question_id = uuid4()
    subject_id = uuid4()
    revision = SimpleNamespace(
        id=revision_id,
        question_id=question_id,
        subject_id=subject_id,
    )
    item = SimpleNamespace(
        question_revision_id=revision_id,
        question_id=question_id,
        subject_id=subject_id,
    )
    content = RevisionContent(
        question_type="short_answer",
        stem_blocks=[ImageBlock(type="image", asset_id=uuid4())],
        options=[],
        explanation_blocks=[],
    )
    image_path = tmp_path / "question.png"
    image_path.write_bytes(_png_bytes())

    class _Access:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def _asset(self, *_args: Any) -> object:
            return object()

        def _asset_source(self, *_args: Any) -> object:
            return object()

        def _answer_safe(self, *_args: Any) -> bool:
            return True

        def _image_path(self, *_args: Any) -> tuple[Any, object]:
            return image_path, object()

    monkeypatch.setattr(
        "backend.app.services.grading.scoring_images.PersonalContentAccess",
        _Access,
    )
    session = SimpleNamespace(get=lambda *_args, **_kwargs: revision)
    images = prepare_personal_scoring_images(
        session,  # type: ignore[arg-type]
        item,  # type: ignore[arg-type]
        content=content,
        profile=object(),
    )
    assert len(images) == 1
    assert images[0].data == image_path.read_bytes()


def test_personal_images_fail_when_source_is_not_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision_id = uuid4()
    question_id = uuid4()
    subject_id = uuid4()
    revision = SimpleNamespace(
        id=revision_id,
        question_id=question_id,
        subject_id=subject_id,
    )
    item = SimpleNamespace(
        question_revision_id=revision_id,
        question_id=question_id,
        subject_id=subject_id,
    )
    content = RevisionContent(
        question_type="short_answer",
        stem_blocks=[ImageBlock(type="image", asset_id=uuid4())],
        options=[],
        explanation_blocks=[],
    )

    class _Access:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def _asset(self, *_args: Any) -> object:
            return object()

        def _asset_source(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(
        "backend.app.services.grading.scoring_images.PersonalContentAccess",
        _Access,
    )
    session = SimpleNamespace(get=lambda *_args, **_kwargs: revision)
    with pytest.raises(GradingImageError) as error:
        prepare_personal_scoring_images(
            session,  # type: ignore[arg-type]
            item,  # type: ignore[arg-type]
            content=content,
            profile=object(),
        )
    assert error.value.error_code == "VISION_SOURCE_UNCONFIRMED"


def test_general_knowledge_rejects_material_versions() -> None:
    with pytest.raises(GradingInputError):
        _source(
            grounding="general_knowledge",
            material_version_ids=(str(uuid4()),),
        )


def test_legacy_practice_without_frozen_material_scope_cannot_expand_to_current_materials():
    from backend.app.services.grading.personal_subjective import PersonalSubjectivePort

    content = RevisionContent(question_type="short_answer", stem_blocks=[{"type": "text", "text": "Explain"}])
    item = SimpleNamespace(subject_id=uuid4(), question_id=uuid4(), session_id=uuid4(), knowledge_point_ids=[])
    answer = SimpleNamespace(id=uuid4(), text_answer="input")
    port = PersonalSubjectivePort(session=None, runtime=None, profile=None)
    with pytest.raises(GradingInputError):
        port._source(item, answer, content, {"question_type": "short_answer",
            "grounding": "uploaded_material", "reference_answer": "reference", "scoring_rubric": "basis"})


@pytest.mark.parametrize("failure", ["provider", "invalid_payload", "cancelled"])
def test_vision_grading_failure_releases_its_async_snapshot_lease(failure):
    from backend.app.ai.vision.base import VisionImage
    from backend.app.services.grading.subjective_grader import (
        InvalidLLMResponseError,
        ProviderFailedError,
        SubjectiveGrader,
    )
    from backend.app.services.settings.runtime_snapshot import SnapshotClientLease
    from tests.support.runtime_consumer_fakes import FakeLLM

    error = asyncio.CancelledError() if failure == "cancelled" else (
        RuntimeError("synthetic vision failure") if failure == "provider" else None)
    provider = FakeLLM("synthetic-vision", error=error, payload=object())
    provider.vision = True
    released = []
    lease = SnapshotClientLease(snapshot_id=uuid4(), task_kind="vision", client=provider,
                                on_release=lambda value: released.append(value))
    runtime = SimpleNamespace(acquire=lambda _kind: lease, retrieval_mode=lambda _kind: "keyword_only",
                              record_invocation=lambda *_args, **_kwargs: None)
    expected = asyncio.CancelledError if failure == "cancelled" else (
        ProviderFailedError if failure == "provider" else InvalidLLMResponseError)
    with pytest.raises(expected):
        asyncio.run(SubjectiveGrader().grade(None, _source(grounding="general_knowledge"),
            max_score=10, images=(VisionImage.from_bytes(_png_bytes()),), runtime=runtime))
    assert lease.released
    assert provider.closed
    assert released == [lease]
