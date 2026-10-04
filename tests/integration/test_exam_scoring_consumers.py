"""T177 real HTTP/PG fixed basis, rounding and review (TCR section 32)."""

import asyncio
import base64
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.ai.vision.base import ProviderImage
from backend.app.ai.workflows.state import CHECKPOINT_STATE_KEY
from backend.app.domain.enums import ReviewStatus, WorkflowStatus
from backend.app.models import (
    Exam,
    ExamResult,
    GradingResult,
    ReviewRecord,
    WorkflowRun,
)
from backend.app.schemas.exam_scoring import ScoringBasis, ScoringPoint
from backend.app.services.grading import subjective_grader as grading_module
from backend.app.services.grading.grading_context import GradingContextError
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
)
from backend.app.services.grading.subjective_grader import (
    SubjectiveGrader,
)
from backend.app.services.grading.subjective_pipeline import build_subjective_source
from tests.integration.test_fixed_scoring_input import image_fixture
from tests.integration.test_langgraph_grading_workflow import (
    ReviewEnv,
    SequenceScoringProvider,
    _api_client,
    _confirm_body,
    _seed_paper,
    _start,
    _student_result,
    _teacher_headers,
)
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.subjective_grading_doubles import (
    StubEmbeddingProvider,
    StubReranker,
    StubRetriever,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings


class ConfirmedPointProvider(SequenceScoringProvider):
    """Only authors external model output; fixed business decisions stay real."""

    async def generate_structured(self, messages, schema, **kwargs: Any):
        payload = await super().generate_structured(messages, schema, **kwargs)
        return payload.model_copy(
            update={
                "correct_points": ["保存数据（本场确认）"],
                "missing_knowledge_points": [],
            }
        )


@pytest.fixture
def environment():
    with isolated_postgres_engine() as engine, Session(engine) as session:
        paper = _seed_paper(session, "fixed-consumer", solo=True)
        exam = session.get(Exam, UUID(paper.exam_id))
        link = exam.exam_question_links[0]
        basis = ScoringBasis.model_validate(link.scoring_basis)
        basis = basis.model_copy(
            update={
                "additive": True,
                "rounding_delta": Decimal("0.00"),
                "points": [
                    ScoringPoint(
                        key="saving",
                        label="保存数据（本场确认）",
                        base_points=Decimal("10.00"),
                        default_points=Decimal("3.33"),
                        confirmed_points=Decimal("3.33"),
                    )
                ],
            }
        )
        link.score = Decimal("3.33")
        link.base_score = Decimal("10.00")
        link.scoring_basis = basis.model_dump(mode="json")
        link.published_knowledge_points = ["发布标签"]
        # Legal metadata maintenance must not change published grading labels.
        link.question.knowledge_points = ["当前题库新标签"]
        assert link.question.score == Decimal("10.00")
        session.commit()
        yield ReviewEnv(engine, paper)


def test_formal_http_rounds_once_uses_fixed_standard_and_persists_real_inputs(
    environment,
):
    provider = ConfirmedPointProvider([0.95], score=Decimal("0.005"))
    with _api_client(environment, scoring_provider=provider) as harness:
        started = _start(harness, environment)
        result = _student_result(harness, environment)
    assert started.status_code == 200
    assert started.json()["status"] == WorkflowStatus.COMPLETED.value
    assert result.status_code == 200 and result.json()["is_final"] is True
    assert Decimal(result.json()["total_score"]) == Decimal("0.01")
    assert len(provider.calls) == 1
    prompt = str(provider.calls[0]["messages"])
    assert "保存数据（本场确认）" in prompt and "3.33" in prompt
    with Session(environment.engine) as session:
        grading = session.scalars(select(GradingResult)).one()
        assert grading.score == Decimal("0.01")
        assert grading.max_score == Decimal("3.33")
        assert grading.knowledge_points == ["发布标签"]
        run = session.scalars(select(WorkflowRun)).one()
        data = run.checkpoint[CHECKPOINT_STATE_KEY]["scoring_inputs"][
            environment.paper.subjective_answer_ids[0]
        ]
        assert data["effective_score"] == "3.33"
        assert data["published_knowledge_points"] == ["发布标签"]
        assert data["course_context"] and data["source_references"]
        assert (
            run.checkpoint[CHECKPOINT_STATE_KEY]["grading_results"][
                environment.paper.subjective_answer_ids[0]
            ]["exam_question_id"]
            == data["exam_question_id"]
        )


def test_manual_review_checks_raw_limit_before_rounding_and_keeps_row_identity(
    environment,
):
    provider = ConfirmedPointProvider([0.3], score=Decimal("1.005"))
    with _api_client(environment, scoring_provider=provider) as harness:
        started = _start(harness, environment)
        assert started.status_code == 200
        assert started.json()["status"] == WorkflowStatus.PAUSED.value
        pending = _student_result(harness, environment).json()
        assert pending["total_score"] is None and pending["is_final"] is False
        with Session(environment.engine) as session:
            row_id = session.scalars(select(GradingResult)).one().id
        body = _confirm_body(environment, started.json()["workflow_id"])
        body.update(
            action="modify", score="3.3300000000000000000001", reason="合成复核边界"
        )
        over = harness.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(environment), json=body
        )
        assert over.status_code in {409, 422}
        with Session(environment.engine) as session:
            assert list(session.scalars(select(ReviewRecord))) == []
            assert (
                session.scalars(select(GradingResult)).one().review_status
                is ReviewStatus.PENDING_REVIEW
            )
        body["score"] = "2.345"
        changed = harness.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(environment), json=body
        )
        assert changed.status_code == 200 and changed.json()["decision_saved"] is True
        result = _student_result(harness, environment).json()
        assert result["is_final"] is True and Decimal(result["total_score"]) == Decimal(
            "2.35"
        )
    with Session(environment.engine) as session:
        row = session.scalars(select(GradingResult)).one()
        assert row.id == row_id and row.max_score == Decimal("3.33")
        assert row.score == Decimal("2.35") and row.knowledge_points == ["发布标签"]
        assert len(list(session.scalars(select(ReviewRecord)))) == 1
    assert len(provider.calls) == 1


def test_raw_model_score_above_current_limit_is_not_rounded_into_success(environment):
    provider = ConfirmedPointProvider([0.95], score=Decimal("3.3300000000000000000001"))
    with _api_client(environment, scoring_provider=provider) as harness:
        started = _start(harness, environment)
        result = _student_result(harness, environment)
    assert (
        started.status_code == 200
        and started.json()["status"] == WorkflowStatus.FAILED.value
    )
    assert result.json()["total_score"] is None and result.json()["items"] == []
    assert len(provider.calls) == 1
    with Session(environment.engine) as session:
        assert list(session.scalars(select(GradingResult))) == []
        assert list(session.scalars(select(ExamResult))) == []
        run = session.scalars(select(WorkflowRun)).one()
        assert "GRADING_SCORE_OUT_OF_RANGE" in str(run.checkpoint)


class OwnedVisionProvider(ConfirmedPointProvider):
    def __init__(self, *, capable=True):
        super().__init__([0.95], score=Decimal("2.005"))
        self.capable = capable
        self.closed = False

    def supports_vision(self):
        return self.capable

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("failure", [None, "unsupported", "missing_after_read"])
def test_real_image_bytes_and_current_conditions_enter_vision_only(
    environment, tmp_path, monkeypatch, failure
):
    with Session(environment.engine) as session:
        question_id = (
            session.get(Exam, UUID(environment.paper.exam_id))
            .exam_question_links[0]
            .question_id
        )
    author = SimpleNamespace(
        objective_question_id=question_id, teacher_id=UUID(environment.paper.teacher_id)
    )
    path, asset_id, check_id = image_fixture((environment.engine, author), tmp_path)
    with Session(environment.engine) as session:
        snapshot = DatabaseGradingSubmissionReader(session=session, root=tmp_path).load(
            environment.paper.submission_id
        )
        snapshot.require_scoring_ready()
        target = snapshot.answers[0]
        source = build_subjective_source(snapshot, target)
        vision = OwnedVisionProvider(capable=failure != "unsupported")
        text = SequenceScoringProvider([0.95])
        monkeypatch.setattr(
            grading_module, "create_vision_provider", lambda settings: vision
        )
        if failure == "missing_after_read":
            path.unlink()
        grader = SubjectiveGrader(provider=text)
        command = grader.grade(
            session,
            source,
            max_score=target.max_score,
            settings=build_test_settings(storage_root=tmp_path),
            retriever=StubRetriever(
                [make_chunk(course_id=environment.paper.course_id)]
            ),
            reranker=StubReranker(),
            embedding_provider=StubEmbeddingProvider(),
        )
        if failure:
            with pytest.raises(GradingContextError) as error:
                asyncio.run(command)
            assert error.value.error_code == (
                "VISION_NOT_SUPPORTED" if failure == "unsupported" else "FILE_MISSING"
            )
            assert vision.calls == []
        else:
            result = asyncio.run(command)
            assert result.score == Decimal("2.01") and result.max_score == Decimal(
                "3.33"
            )
            assert result.exam_question_id == target.scoring_input.exam_question_id
            assert len(vision.calls) == 1
            parts = vision.calls[0]["messages"][-1]["content"]
            image_part = next(part for part in parts if part["type"] == "image")
            assert isinstance(image_part["image"], ProviderImage)
            assert base64.b64decode(image_part["image"].value) == path.read_bytes()
            assert image_part["image"].mime_type == "image/png"
            assert "Synthetic reviewed condition" in str(parts)
            assert source.scoring_input.verified_image_conditions.check.id == check_id
            assert source.scoring_input.assets[0].id == asset_id
        assert text.calls == []
        assert vision.closed is True
        serialized = snapshot.scoring_inputs[target.answer_id].model_dump_json()
        assert "iVBOR" not in serialized and str(path) not in serialized
