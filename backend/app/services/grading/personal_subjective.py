"""Personal subjective grading adapter.

The adapter keeps the personal-practice boundary explicit:

- the question, reference answer, rubric and grounding were frozen when the
  practice started;
- retrieval is limited to the personal subject and the frozen material
  versions/documents;
- general-knowledge items perform no retrieval and carry no fabricated source;
- images come from the frozen revision's managed assets only;
- every model call uses the immutable ``RuntimeSnapshotContext`` and its client
  lease; a failure becomes an honest failed round, never a zero score.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.practice import PracticeAnswer, PracticeItem
from backend.app.models.subject import KnowledgePoint
from backend.app.schemas.content_blocks import RevisionContent
from backend.app.schemas.retrieval_scope import RetrievalScope
from backend.app.services.grading.grading_context import (
    GradingInputError,
    SubjectiveGradingSource,
)
from backend.app.services.grading.scoring_images import (
    GradingImageError,
    prepare_personal_scoring_images,
)
from backend.app.services.grading.subjective_grader import SubjectiveGrader
from backend.app.services.personal_grading import SubjectiveResult
from backend.app.services.settings.runtime_snapshot import RunSnapshotContext

SUBJECTIVE_FAILURE_CODE = "SUBJECTIVE_GRADING_FAILED"


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return tuple(dict.fromkeys(result))


def _uuids(value: object) -> tuple[str, ...]:
    result: list[str] = []
    for text in _strings(value):
        try:
            result.append(str(UUID(text)))
        except ValueError:
            raise GradingInputError("本场冻结范围包含非法标识。") from None
    return tuple(dict.fromkeys(result))


def _frozen_content(frozen: Mapping[str, Any]) -> RevisionContent:
    try:
        return RevisionContent.model_validate(
            {
                "question_type": frozen.get("question_type"),
                "stem_blocks": frozen.get("stem_blocks") or [],
                "options": frozen.get("options") or [],
                "explanation_blocks": frozen.get("explanation_blocks") or [],
            }
        )
    except (TypeError, ValueError):
        raise GradingInputError("本场题目内容已损坏，无法组装评分输入。") from None


def _grounding_evidence(frozen: Mapping[str, Any]) -> Mapping[str, Any]:
    refs = frozen.get("grounding_refs")
    return refs if isinstance(refs, Mapping) else {}


def _evidence_versions(frozen: Mapping[str, Any]) -> tuple[str, ...]:
    evidence = _grounding_evidence(frozen).get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, str):
        return ()
    return _uuids(
        [
            entry.get("material_version_id")
            for entry in evidence
            if isinstance(entry, Mapping)
        ]
    )


def _frozen_documents(frozen: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = _uuids(frozen.get("scope_document_ids"))
    if explicit:
        return explicit
    evidence = _grounding_evidence(frozen).get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, str):
        return ()
    return _uuids(
        [
            entry.get("document_id")
            for entry in evidence
            if isinstance(entry, Mapping)
        ]
    )


def _knowledge_point_names(
    session: Session, item: PracticeItem
) -> tuple[str, ...]:
    ids: list[UUID] = []
    for value in item.knowledge_point_ids:
        try:
            ids.append(UUID(str(value)))
        except ValueError:
            continue
    if not ids:
        return ()
    names = session.scalars(
        select(KnowledgePoint.name)
        .where(
            KnowledgePoint.subject_id == item.subject_id,
            KnowledgePoint.id.in_(ids),
        )
        .order_by(KnowledgePoint.name)
    )
    return tuple(dict.fromkeys(name for name in names if name))


@final
class PersonalSubjectivePort:
    """Callable ``SubjectivePort`` bound to one practice grading run."""

    def __init__(
        self,
        *,
        session: Session,
        runtime: RunSnapshotContext,
        profile: object,
    ) -> None:
        self._session = session
        self._runtime = runtime
        self._profile = profile

    def __call__(
        self, item: PracticeItem, answer: PracticeAnswer
    ) -> SubjectiveResult:
        frozen = dict(item.scoring_input or {})
        try:
            content = _frozen_content(frozen)
            source = self._source(item, answer, content, frozen)
            if content.asset_ids(include_explanation=False):
                if self._profile is None:
                    raise GradingImageError(
                        "SNAPSHOT_DEPENDENCY_UNAVAILABLE",
                        "本题包含受管题图，但个人学习空间没有可用文件配置。",
                    )
                images = prepare_personal_scoring_images(
                    self._session,
                    item,
                    content=content,
                    profile=self._profile,
                )
            else:
                images = ()
            result = asyncio.run(
                SubjectiveGrader().grade(
                    self._session,
                    source,
                    max_score=item.fixed_max_score,
                    images=images,
                    runtime=self._runtime,
                )
            )
        except Exception as error:  # noqa: BLE001 - failures must become a failed round.
            return self._failure(error)
        feedback = {
            "correct_points": list(result.correct_points),
            "missing_knowledge_points": list(result.missing_knowledge_points),
            "suggestions": list(result.suggestions),
            "retrieved_context_ids": list(result.retrieved_context_ids),
        }
        if result.review_status == "Pending Review":
            state: str = "needs_review"
        elif result.validation_status == "Failed":
            state = "failed"
        else:
            state = "valid"
        return SubjectiveResult(
            score=Decimal(result.score) if state == "valid" else None,
            reason=result.reason,
            validation_state=state,  # type: ignore[arg-type]
            confidence=float(result.confidence),
            feedback=feedback,
            runtime_snapshot_id=self._runtime.snapshot_id,
        )

    def _source(
        self,
        item: PracticeItem,
        answer: PracticeAnswer,
        content: RevisionContent,
        frozen: Mapping[str, Any],
    ) -> SubjectiveGradingSource:
        if answer.text_answer is None:
            raise GradingInputError("本场主观题没有有效文字作答。")
        grounding = str(frozen.get("grounding") or "")
        if grounding not in {"uploaded_material", "general_knowledge"}:
            raise GradingInputError("本场题目未声明有效评分依据来源。")
        if frozen.get("scope_frozen"):
            document_ids = () if grounding == "general_knowledge" else _uuids(frozen.get("scope_document_ids"))
            versions = () if grounding == "general_knowledge" else _uuids(frozen.get("scope_material_version_ids"))
            if grounding == "uploaded_material" and not document_ids:
                raise GradingInputError("本场没有冻结的教学资料，不能扩大检索到当前科目全部资料。")
        else:
            document_ids = () if grounding == "general_knowledge" else _frozen_documents(frozen)
            versions = () if grounding == "general_knowledge" else (
                _uuids(frozen.get("scope_material_version_ids")) or _evidence_versions(frozen)
            )
        if grounding == "uploaded_material" and not (document_ids or versions):
            raise GradingInputError("本场缺少已冻结的资料范围，不能改用当前科目全部教材。")
        scope = (
            RetrievalScope(document_ids=tuple(UUID(value) for value in document_ids))
            if document_ids
            else RetrievalScope()
        )
        return SubjectiveGradingSource(
            question_type=frozen.get("question_type"),
            subject_id=item.subject_id,
            question_content=content.text_projection(),
            reference_answer=str(frozen.get("reference_answer") or ""),
            student_answer=answer.text_answer,
            scoring_rubric=frozen.get("scoring_rubric"),
            knowledge_points=_knowledge_point_names(self._session, item),
            material_version_ids=versions,
            question_id=item.question_id,
            answer_id=answer.id,
            submission_id=item.session_id,
            retrieval_scope=scope,
            grounding=grounding,  # type: ignore[arg-type]
        )

    def _failure(self, error: Exception) -> SubjectiveResult:
        code = (
            getattr(error, "error_code", None)
            or getattr(error, "code", None)
            or SUBJECTIVE_FAILURE_CODE
        )
        return SubjectiveResult(
            score=None,
            reason=f"主观题评分未完成：{code}。",
            validation_state="failed",
            error_code=str(code),
            runtime_snapshot_id=self._runtime.snapshot_id,
        )


__all__ = ["PersonalSubjectivePort"]
