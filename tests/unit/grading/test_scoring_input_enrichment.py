"""T177 per-call retrieval context survives the background executor (TCR §32)."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    GradingTaskStatus,
    GradingTaskStatusDTO,
    ScoringSourceReference,
)
from backend.app.services.grading.confidence_policy import ConfidenceDecision
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    DefaultScoringPipeline,
    InlineGradingTaskExecutor,
)
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.unit.models.sqlite_support import create_sqlite_engine, seed_submission


class RetrievalScorer:
    def __call__(self, snapshot, target):
        raise AssertionError("The per-call input sink was not used.")

    def score_with_input(self, snapshot, target, *, on_scoring_input):
        value = target.scoring_input.model_copy(
            update={
                "course_context": "Fixture actual retrieved text",
                "source_references": [
                    ScoringSourceReference(
                        chunk_id="fixture-actual-chunk",
                        course_id=snapshot.course_id,
                        document_id=None,
                        metadata={"source": "controlled-retrieval"},
                    )
                ],
            }
        )
        on_scoring_input(value)
        return GradingResult(
            question_type=target.question_type,
            score=Decimal("6.00"),
            max_score=value.effective_score,
            reason="Fixture verified grading",
            correct_points=[],
            missing_knowledge_points=[],
            knowledge_points=value.published_knowledge_points,
            suggestions=[],
            confidence=0.9,
            answer_id=value.answer_id,
            submission_id=value.submission_id,
            exam_question_id=value.exam_question_id,
            retrieved_context_ids=["fixture-actual-chunk"],
        ), ConfidenceDecision(
            confidence=0.9,
            threshold=0.8,
            requires_review=False,
            review_status="Not Required",
            grading_status="Accepted",
            reason="Fixture current decision",
        )


def test_worker_preserves_per_call_actual_retrieval_input_without_replacing_it():
    engine = create_sqlite_engine()
    try:
        with Session(engine) as session:
            info = seed_submission(session)
            confirm_synthetic_exam_basis(session, info.exam_id)
            session.commit()
        reader = DatabaseGradingSubmissionReader(
            session_factory=lambda: Session(engine)
        )
        repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
        snapshot = reader.load(str(info.submission_id))
        task = GradingTaskStatusDTO(
            task_id=str(uuid4()),
            submission_id=snapshot.submission_id,
            status=GradingTaskStatus.QUEUED,
            durable=True,
            created_at=datetime.now(UTC),
        )
        repository.save_task(
            task,
            request_id="fixture-retrieval-input",
            scoring_inputs=snapshot.scoring_inputs,
        )
        pipeline = DefaultScoringPipeline(subjective_scorer=RetrievalScorer())
        executor = InlineGradingTaskExecutor(
            repository=repository, reader=reader, pipeline=pipeline
        )
        executor.execute(task.task_id, snapshot.submission_id)
        assert repository.get_task(task.task_id).status is GradingTaskStatus.COMPLETED
        saved = repository.get_scoring_inputs(task.task_id)
        subjective = saved[str(info.subjective_answer_id)]
        assert subjective.course_context == "Fixture actual retrieved text"
        assert [ref.chunk_id for ref in subjective.source_references] == [
            "fixture-actual-chunk"
        ]
        assert saved[str(info.objective_answer_id)].course_context is None
        actual = repository.get_exam_result(snapshot.submission_id)
        assert actual.is_final and actual.final_total_score == Decimal("16.00")
        assert actual.items[1].retrieved_context_ids == ["fixture-actual-chunk"]
    finally:
        engine.dispose()
