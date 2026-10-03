"""T165: true source relationships, exact score and atomic adaptation behavior."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from backend.app.ai.agents.question_agent import QuestionAgent
from backend.app.ai.agents.state import QuestionGenerationRequest
from backend.app.api.question_generation import QuestionGenerationService
from backend.app.domain.enums import (
    DocumentStatus,
    QuestionSourceType,
    QuestionStatus,
    QuestionType,
    UserRole,
)
from backend.app.models import (
    Base,
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Question,
    QuestionGenerationMetadata,
    QuestionSourceChunk,
)
from tests.support.question_generation_doubles import (
    StubEmbeddingProvider,
    StubQuestionProvider,
    StubRetriever,
    make_candidate,
    make_chunk,
)
from tests.unit.services.test_submission_service import add_user
from tests.unit.settings_helpers import build_test_settings


@pytest.mark.parametrize("score", ["0", "-1", "2.001", "1000000", "NaN"])
def test_target_score_rejects_unstorable_values(score: str) -> None:
    with pytest.raises(ValueError):
        QuestionGenerationRequest(course_id="course", target_score=score)


def test_target_score_is_a_real_generation_requirement() -> None:
    from backend.app.ai.agents.question_agent import build_generation_query

    request = QuestionGenerationRequest(course_id="course", target_score="3.50")
    assert request.target_score == Decimal("3.50")
    assert "3.50" in build_generation_query(request)


@pytest.fixture
def database() -> Any:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_fk(connection: Any, _record: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        teacher = add_user(
            session, UserRole.TEACHER, username="t165-teacher", email="t165@example.com"
        )
        course = Course(name="T165 course", creator=teacher)
        session.add(course)
        session.flush()
        kb = KnowledgeBase(course_id=course.id, name="Teaching evidence")
        session.add(kb)
        session.flush()
        document = Document(
            course_id=course.id,
            knowledge_base_id=kb.id,
            uploaded_by=teacher.id,
            original_filename="lesson.txt",
            file_format="txt",
            status=DocumentStatus.READY,
        )
        session.add(document)
        session.flush()
        chunk = DocumentChunk(
            document_id=document.id,
            course_id=course.id,
            knowledge_base_id=kb.id,
            chunk_index=0,
            content="Variables store data.",
            chunk_metadata={},
        )
        parent = Question(
            course_id=course.id,
            type=QuestionType.SINGLE_CHOICE,
            content="Original question",
            options={"C": "third", "A": "first"},
            reference_answer="C",
            analysis="Original analysis",
            scoring_rubric="Correct gets 2 points.",
            score=Decimal(2),
            status=QuestionStatus.APPROVED,
            source_type=QuestionSourceType.MANUAL,
            created_by=teacher.id,
        )
        session.add_all([chunk, parent])
        session.commit()
        info = {
            "actor_id": str(teacher.id),
            "course_id": str(course.id),
            "parent_id": str(parent.id),
            "chunk_id": str(chunk.id),
            "document_id": str(document.id),
        }
    yield engine, info
    engine.dispose()


def generate(
    database: Any, *, score: float = 3, citations: bool = True, provider: Any = None
) -> tuple[Any, Any]:
    engine, info = database
    item = make_candidate(
        score=score,
        scoring_rubric="正确选项得 3 分，其他选项不得分。",
        source_context_ids=[info["chunk_id"]] if citations else [],
        analysis="New independent explanation",
    )
    provider = provider or StubQuestionProvider(candidates=[item])
    service = QuestionGenerationService(
        session_factory=lambda: Session(engine),
        agent=QuestionAgent(provider=provider),
        retriever=StubRetriever(
            [
                make_chunk(
                    info["chunk_id"],
                    course_id=info["course_id"],
                    document_id=info["document_id"],
                )
            ]
        ),
        embedding_provider=StubEmbeddingProvider(),
        settings=build_test_settings(),
    )
    return asyncio.run(
        service.adapt_candidates(
            course_id=info["course_id"],
            actor_id=info["actor_id"],
            request_id="t165",
            source_question_id=info["parent_id"],
            adaptation_type="rewrite",
            target_score=Decimal(3),
        )
    ), provider


def test_adaptation_has_new_question_parent_and_real_teaching_sources(
    database: Any,
) -> None:
    from backend.app.models import QuestionSourcePaper

    engine, info = database
    response, provider = generate(database)
    candidate = response.candidates[0]
    assert candidate.source_type == QuestionSourceType.ADAPTED
    assert candidate.analysis == "New independent explanation"
    assert candidate.sources_persisted and candidate.source_status == "persisted"
    assert candidate.parent_sources[0]["source_question_id"] == info["parent_id"]
    with Session(engine) as session:
        parent = session.get(Question, UUID(info["parent_id"]))
        assert (
            parent is not None
            and parent.content == "Original question"
            and list(parent.options) == ["C", "A"]
        )
        assert parent.status == QuestionStatus.APPROVED
        edge = session.scalars(select(QuestionSourcePaper)).one()
        assert str(edge.derived_question_id) == candidate.candidate_id
        assert str(edge.source_question_id) == info["parent_id"]
        assert (
            session.scalar(select(func.count()).select_from(QuestionSourceChunk)) == 1
        )
        assert (
            session.scalar(select(func.count()).select_from(QuestionGenerationMetadata))
            == 1
        )
    prompt = provider.calls[0]["messages"][-1]["content"]
    assert "Original question" in prompt and prompt.index('"C"') < prompt.index('"A"')


def test_target_score_mismatch_is_not_silently_overwritten(database: Any) -> None:
    with pytest.raises(Exception, match="QUESTION_TARGET_SCORE_MISMATCH"):
        generate(database, score=2)
    engine, _ = database
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Question)) == 1


def test_adaptation_missing_teaching_citation_cannot_create_fake_parent_evidence(
    database: Any,
) -> None:
    response, _ = generate(database, citations=False)
    candidate = response.candidates[0]
    assert candidate.status == QuestionStatus.NEEDS_REVISION
    assert candidate.parent_sources and not candidate.sources_persisted
    assert candidate.source_status == "no_sources"


def test_parent_edge_write_failure_rolls_back_all_candidate_records(
    database: Any,
) -> None:
    from backend.app.models import QuestionSourcePaper

    engine, _ = database

    def fail(*_args: Any) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        raise SQLAlchemyError("injected parent persistence failure")

    event.listen(QuestionSourcePaper, "before_insert", fail)
    try:
        with pytest.raises(Exception, match="QUESTION_CANDIDATE_STORE_NOT_READY"):
            generate(database)
    finally:
        event.remove(QuestionSourcePaper, "before_insert", fail)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Question)) == 1
        for model in (
            QuestionSourceChunk,
            QuestionGenerationMetadata,
            QuestionSourcePaper,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_parent_changed_during_call_rejects_stale_adaptation(database: Any) -> None:
    engine, info = database

    class ChangingProvider(StubQuestionProvider):
        async def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
            with Session(engine) as session:
                parent = session.get(Question, UUID(info["parent_id"]))
                assert parent is not None
                parent.content = "Changed while generating"
                parent.validation_revision += 1
                session.commit()
            return await super().generate_structured(*args, **kwargs)

    provider = ChangingProvider(
        candidates=[make_candidate(score=3, source_context_ids=[info["chunk_id"]])]
    )
    with pytest.raises(Exception, match="QUESTION_ADAPTATION_STALE"):
        generate(database, provider=provider)


def test_parent_graph_rejects_self_cross_course_and_cycle(database: Any) -> None:
    from backend.app.models import QuestionSourcePaper
    from backend.app.services.question_adaptation_service import (
        QuestionAdaptationService,
    )

    engine, info = database
    with Session(engine) as session:
        parent = session.get(Question, UUID(info["parent_id"]))
        assert parent is not None
        derived = Question(
            course_id=parent.course_id,
            type=parent.type,
            content="Derived",
            score=2,
            created_by=parent.created_by,
        )
        other_course = Course(name="Other", created_by=parent.created_by)
        session.add_all([derived, other_course])
        session.flush()
        other = Question(
            course_id=other_course.id,
            type=parent.type,
            content="Other course",
            score=2,
            created_by=parent.created_by,
        )
        session.add(other)
        session.flush()
        service = QuestionAdaptationService(session)
        with pytest.raises(Exception, match="QUESTION_ADAPTATION_SELF_REFERENCE"):
            service.check_edge(
                course_id=parent.course_id,
                derived_question_id=parent.id,
                source_question_id=parent.id,
            )
        with pytest.raises(Exception, match="QUESTION_ADAPTATION_PARENT_INVALID"):
            service.check_edge(
                course_id=parent.course_id,
                derived_question_id=derived.id,
                source_question_id=other.id,
            )
        session.add(
            QuestionSourcePaper(
                derived_question_id=derived.id,
                source_question_id=parent.id,
                adaptation_type="rewrite",
            )
        )
        session.flush()
        with pytest.raises(Exception, match="QUESTION_ADAPTATION_CYCLE"):
            service.check_edge(
                course_id=parent.course_id,
                derived_question_id=parent.id,
                source_question_id=derived.id,
            )


def add_parent_image(database: Any) -> str:
    from io import BytesIO

    from PIL import Image

    from backend.app.services.question_asset_service import QuestionAssetService

    engine, info = database
    stream = BytesIO()
    Image.new("RGB", (32, 24), "white").save(stream, format="PNG")
    with Session(engine) as session:
        parent = session.get(Question, UUID(info["parent_id"]))
        assert parent is not None
        parent.status = QuestionStatus.DRAFT
        session.commit()
        asset = QuestionAssetService(session).upload_question(
            parent.id,
            content=stream.getvalue(),
            asset_type="diagram",
            caption="Original pixels",
            actor_id=parent.created_by,
            student_visible=True,
        )
        parent.status = QuestionStatus.APPROVED
        session.commit()
        return str(asset.id)


def test_adaptation_reuses_original_bytes_with_new_identity_and_no_inherited_review(
    database: Any,
) -> None:
    from backend.app.models import QuestionAsset

    engine, _info = database
    original_asset_id = add_parent_image(database)
    response, provider = generate(database)
    with Session(engine) as session:
        original = session.get(QuestionAsset, UUID(original_asset_id))
        derived = session.get(Question, UUID(response.candidates[0].candidate_id))
        assert original is not None and derived is not None
        assert len(derived.assets) == 1
        new = derived.assets[0]
        assert new.id != original.id and new.file_id != original.file_id
        assert (
            new.storage_path == original.storage_path
            and new.file_metadata == original.file_metadata
        )
        assert (
            new.region == original.region
            and new.source_page_id == original.source_page_id
        )
        assert original.student_visible is True and new.student_visible is False
        assert (
            derived.image_assessment["runs"] == []
            and derived.image_assessment["manual_checks"] == []
        )
        assert (
            derived.frozen_at is None
            and derived.status == QuestionStatus.PENDING_REVIEW
        )
    prompt = provider.calls[0]["messages"][-1]["content"]
    assert "storage_path" not in prompt and "file_metadata" not in prompt


def test_reused_asset_failure_preserves_parent_bytes_and_atomic_candidate_boundary(
    database: Any,
) -> None:
    from backend.app.models import QuestionAsset, QuestionSourcePaper
    from backend.app.services.file_storage_service import FileStorageService

    engine, _info = database
    asset_id = add_parent_image(database)
    with Session(engine) as session:
        original = session.get(QuestionAsset, UUID(asset_id))
        assert original is not None
        path = FileStorageService(session).resolve_path(original.storage_path)
        before = path.read_bytes()

    def fail(*_args: Any) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        raise SQLAlchemyError("injected asset failure")

    event.listen(QuestionAsset, "before_insert", fail)
    try:
        with pytest.raises(Exception, match="QUESTION_CANDIDATE_STORE_NOT_READY"):
            generate(database)
    finally:
        event.remove(QuestionAsset, "before_insert", fail)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Question)) == 1
        assert session.scalar(select(func.count()).select_from(QuestionAsset)) == 1
        assert (
            session.scalar(select(func.count()).select_from(QuestionSourcePaper)) == 0
        )
    assert path.read_bytes() == before
    import json

    receipts = [
        json.loads(file.read_text(encoding="utf-8"))
        for file in path.parent.glob("*.receipt.json")
    ]
    assert any(item["stage"] == "failed" for item in receipts)


def test_locator_only_change_does_not_invalidate_same_parent_input(
    database: Any,
) -> None:
    from backend.app.models import QuestionAsset
    from backend.app.services.file_storage_service import FileStorageService

    engine, info = database
    asset_id = add_parent_image(database)

    class RelocatingProvider(StubQuestionProvider):
        async def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
            with Session(engine) as session:
                asset = session.get(QuestionAsset, UUID(asset_id))
                assert asset is not None
                files = FileStorageService(session)
                original = files.resolve_path(asset.storage_path)
                relocated = original.with_name("relocated-" + original.name)
                relocated.write_bytes(original.read_bytes())
                asset.storage_path = relocated.relative_to(files.root).as_posix()
                session.commit()
            return await super().generate_structured(*args, **kwargs)

    provider = RelocatingProvider(
        candidates=[make_candidate(score=3, source_context_ids=[info["chunk_id"]])]
    )
    response, _provider = generate(database, provider=provider)
    with Session(engine) as session:
        source = session.get(QuestionAsset, UUID(asset_id))
        child = session.get(Question, UUID(response.candidates[0].candidate_id))
        assert source is not None and child is not None
        assert source.storage_path == child.assets[0].storage_path
