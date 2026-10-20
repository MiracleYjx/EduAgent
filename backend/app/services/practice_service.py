"""Personal practice preview, strict history projection and immutable start facts."""

import random
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.practice import (
    GradeAttempt,
    PracticeAnswer,
    PracticeItem,
    PracticeSession,
)
from backend.app.models.question import Question
from backend.app.models.question_origin import QuestionOrigin
from backend.app.models.question_revision import (
    QuestionRevision,
    question_revision_knowledge_points,
)
from backend.app.models.score_dispute import ScoreDispute
from backend.app.models.subject import KnowledgePoint, Subject
from backend.app.schemas.personal_practice import (
    PersonalPracticeView,
    PracticeAnswerInput,
    PracticeAnswerView,
    PracticeAttemptView,
    PracticeConditions,
    PracticeItemResult,
    PracticeItemView,
    PracticePreviewChanged,
    PracticePreviewItem,
    PracticePreviewView,
    PracticeReport,
    PracticeScoringFeedback,
    PracticeStartRequest,
    PracticeSubmitRequest,
)
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import digest
from backend.app.services.personal_grading import (
    SUBJECTIVE_TYPE,
    PracticeGrade,
    SubjectiveResult,
    grade_practice,
    stored_facts,
)
from backend.app.services.personal_question_service import PersonalQuestionService
from backend.app.services.practice_selection import (
    SelectionCandidate,
    select_candidates,
)
from backend.app.services.question_asset_access import PersonalContentAccess
from backend.app.services.question_identity import canonical_id
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotError
from backend.app.services.subject_service import SubjectError

ZERO = Decimal(0)
ONE = Decimal(1)
RATE_QUANTUM = Decimal("0.0001")


class PracticeService(PersonalQuestionService):
    def _practice(
        self, session: Session, identity: UUID, *, lock=False
    ) -> PracticeSession:
        self.space._check_space()
        row = session.get(
            PracticeSession, identity, with_for_update=lock, populate_existing=lock
        )
        if row is None:
            raise MaterialError("PRACTICE_NOT_FOUND", 404)
        return row

    def _grading_port(
        self,
        session: Session,
        practice_id: UUID,
        *,
        regrade: bool,
        item_ids: tuple[UUID, ...] | None = None,
    ):
        """Build the subjective port only when a real model call may be needed."""

        if self.space.subjective_port is not None:
            return self.space.subjective_port, None
        if self.space.config is None or self.space.runtime_snapshots is None:
            return None, None
        query = select(PracticeItem).where(PracticeItem.session_id == practice_id)
        if item_ids is not None:
            query = query.where(PracticeItem.id.in_(item_ids))
        items = list(session.scalars(query.order_by(PracticeItem.position)))
        needs_model = False
        for item in items:
            if str((item.scoring_input or {}).get("question_type")) != SUBJECTIVE_TYPE:
                continue
            state = session.scalar(
                select(PracticeAnswer.answer_state).where(
                    PracticeAnswer.practice_item_id == item.id
                )
            )
            if state == "answered":
                needs_model = True
                break
        if not needs_model:
            return None, None
        if self.space.grading_operations_required:
            # This route has no confirmed OperationRun yet. Never bypass the
            # established paid-send, budget and durable accounting boundary.
            raise MaterialError("GRADING_OPERATION_REQUIRED", 409)
        try:
            runtime = self.space.runtime_snapshots.open_run(
                workflow_id=f"personal-practice:{practice_id}",
                request_id=f"{'regrade' if regrade else 'submit'}:{uuid4()}",
                task_kinds=("subjective_grading",),
                optional_task_kinds=("embedding", "rerank", "vision"),
                storage_root=self.space.storage_root,
            )
        except RuntimeSnapshotError as error:
            failure_code = str(error.code)
            def unavailable(_item, _answer):
                return SubjectiveResult(
                    score=None,
                    reason="主观题评分运行配置不可用，未生成分数。",
                    validation_state="failed",
                    error_code=failure_code,
                )

            return unavailable, None
        from backend.app.services.grading.personal_subjective import (
            PersonalSubjectivePort,
        )

        return (
            PersonalSubjectivePort(
                session=session,
                runtime=runtime,
                profile=self.space.profile,
            ),
            runtime,
        )

    def _grade(
        self,
        session: Session,
        practice_id: UUID,
        *,
        blank_confirmed: bool,
        regrade: bool = False,
        item_ids: tuple[UUID, ...] | None = None,
    ) -> PracticeGrade:
        """Grade through the explicit port, releasing every client lease."""

        if not regrade and stored_facts(session, practice_id):
            return grade_practice(
                session,
                practice_id=practice_id,
                blank_confirmed=blank_confirmed,
            )
        port, runtime = self._grading_port(
            session,
            practice_id,
            regrade=regrade,
            item_ids=item_ids,
        )
        try:
            return grade_practice(
                session,
                practice_id=practice_id,
                blank_confirmed=blank_confirmed,
                subjective=port,
                regrade=regrade,
                item_ids=item_ids,
            )
        finally:
            if runtime is not None:
                runtime.release_all()

    def _history(self, session: Session, subject_id: UUID):
        rows = session.execute(
            select(PracticeAnswer, PracticeItem)
            .join(
                PracticeItem,
                PracticeItem.id == PracticeAnswer.practice_item_id,
            )
            .where(
                PracticeItem.subject_id == subject_id,
                PracticeAnswer.answer_state == "answered",
                PracticeAnswer.submission_state == "submitted",
            )
            .order_by(
                PracticeAnswer.submitted_at,
                PracticeAnswer.created_at,
                PracticeAnswer.id,
            )
        ).all()
        attempted: set[UUID] = set()
        wrong: set[UUID] = set()
        first: dict[UUID, tuple[Decimal, list[str]]] = {}
        for answer, item in rows:
            disputes = list(
                session.scalars(
                    select(ScoreDispute).where(ScoreDispute.answer_id == answer.id)
                )
            )
            if any(dispute.state != "resolved" for dispute in disputes):
                continue
            latest = session.scalar(
                select(GradeAttempt)
                .where(GradeAttempt.answer_id == answer.id)
                .order_by(GradeAttempt.round.desc())
                .limit(1)
            )
            if (
                latest is None
                or latest.validation_state != "valid"
                or latest.score is None
                or not latest.max_score
            ):
                continue
            if (
                disputes
                and max(
                    disputes, key=lambda d: (d.created_at, d.id)
                ).accepted_attempt_id
                != latest.id
            ):
                continue
            root = canonical_id(session, item.question_id, subject_id)
            attempted.add(root)
            ratio = latest.score / latest.max_score
            if ratio < 1:
                wrong.add(root)
            first.setdefault(root, (ratio, item.knowledge_point_ids))
        samples: dict[UUID, list[Decimal]] = {}
        for ratio, points in first.values():
            for point in set(points):
                samples.setdefault(UUID(point), []).append(ratio)
        weak = {
            point
            for point, values in samples.items()
            if len(values) >= 5 and sum(values) / len(values) < Decimal("0.6")
        }
        return attempted, wrong, weak

    def _pool(self, session: Session, subject_id: UUID, conditions: PracticeConditions):
        found = set(
            session.scalars(
                select(KnowledgePoint.id).where(
                    KnowledgePoint.subject_id == subject_id,
                    KnowledgePoint.confirmation_state == "confirmed",
                    KnowledgePoint.id.in_(conditions.knowledge_point_ids),
                )
            )
        )
        if found != set(conditions.knowledge_point_ids):
            raise MaterialError("KNOWLEDGE_POINT_NOT_FOUND", 404)
        if conditions.source_document_ids:
            from backend.app.domain.enums import DocumentPurpose
            from backend.app.models.document import Document

            docs = set(
                session.scalars(
                    select(Document.id).where(
                        Document.id.in_(conditions.source_document_ids),
                        Document.subject_id == subject_id,
                        Document.purpose == DocumentPurpose.PAPER_SOURCE,
                    )
                )
            )
            if docs != set(conditions.source_document_ids):
                raise MaterialError("QUESTION_SOURCE_SCOPE_INVALID", 404)
        attempted, wrong, weak = self._history(session, subject_id)
        pairs = session.execute(
            select(Question, QuestionRevision)
            .join(
                QuestionRevision,
                QuestionRevision.id == Question.current_revision_id,
            )
            .where(
                Question.subject_id == subject_id,
                QuestionRevision.subject_id == subject_id,
            )
            .order_by(Question.id)
        ).all()
        candidates = []
        facts = {}
        excluded = 0
        for question, revision in pairs:
            points = set(
                session.scalars(
                    select(
                        question_revision_knowledge_points.c.knowledge_point_id
                    ).where(
                        question_revision_knowledge_points.c.revision_id == revision.id
                    )
                )
            )
            if (
                revision.question_type not in conditions.question_types
                or (
                    conditions.difficulty is not None
                    and revision.difficulty != conditions.difficulty
                )
                or (
                    conditions.knowledge_point_ids
                    and not points & set(conditions.knowledge_point_ids)
                )
            ):
                continue
            source = (
                "ai"
                if question.source_type and question.source_type.value == "ai_generated"
                else "uploaded"
            )
            origins = list(
                session.scalars(
                    select(QuestionOrigin).where(
                        QuestionOrigin.question_id == question.id
                    )
                )
            )
            if conditions.source_document_ids and not {
                o.document_id for o in origins
            } & set(conditions.source_document_ids):
                continue
            if (
                conditions.source == "uploaded"
                and source != "uploaded"
                or conditions.source == "ai"
                and source != "ai"
            ):
                continue
            content = self.content(revision)
            if (
                revision.admission_state != "eligible"
                or not revision.reference_answer
                or revision.question_type == "short_answer"
                and not revision.scoring_rubric
                or not PersonalContentAccess.chart_ready(content, revision)
            ):
                excluded += 1
                continue
            if content.asset_ids():
                # Real managed bytes and stage permission must survive until start.
                if self.space.profile is None:
                    excluded += 1
                    continue
                access = PersonalContentAccess(session, self.space.profile)
                try:
                    for asset_id in content.asset_ids():
                        asset = access._asset(revision, content, asset_id)
                        access._image_path(asset)
                        if asset_id in content.asset_ids(
                            include_explanation=False
                        ) and not access._answer_safe(asset):
                            raise MaterialError("CONTENT_ASSET_STAGE_FORBIDDEN", 409)
                except (
                    MaterialError,
                    FileStorageError,
                    SubjectError,
                    ValueError,
                    OSError,
                ):
                    excluded += 1
                    continue
            root = canonical_id(session, question.id, subject_id)
            candidate = SelectionCandidate(
                question_id=question.id,
                canonical_id=root,
                revision_id=revision.id,
                knowledge_point_ids=frozenset(points),
                attempted=root in attempted,
                original_wrong=root in wrong,
                source_kind=source,
            )
            candidates.append(candidate)
            facts[str(question.id)] = {
                "question_id": str(question.id),
                "canonical_id": str(root),
                "revision_id": str(revision.id),
                "question_type": revision.question_type,
                "source_kind": source,
                "grounding": revision.grounding,
                "knowledge_point_ids": sorted(map(str, points)),
                "default_score": format(Decimal(str(revision.default_score)), ".2f"),
                "attempted": candidate.attempted,
                "original_wrong": candidate.original_wrong,
            }
        return candidates, facts, excluded, weak

    def _build(
        self,
        session: Session,
        row: PracticeSession,
        *,
        conditions=None,
        allowed_ai=None,
    ) -> PracticePreviewView:
        old = row.conditions or {}
        selected_conditions = conditions or PracticeConditions.model_validate(
            old["request"]
        )
        candidates, facts, excluded, weak = self._pool(
            session, row.subject_id, selected_conditions
        )
        allowed = (
            set(map(UUID, old.get("allowed_ai_ids", [])))
            if allowed_ai is None
            else allowed_ai
        )
        seed = old.get("seed", uuid4().int)
        result = select_candidates(
            candidates,
            selected_conditions,
            weak_points=weak,
            allowed_ai_ids=allowed,
            rng=random.Random(seed),
        )
        visible_pool = [
            c
            for c in candidates
            if selected_conditions.source == "ai"
            or c.source_kind == "uploaded"
            or c.question_id in allowed
        ]
        roots_uploaded = {
            c.canonical_id for c in visible_pool if c.source_kind == "uploaded"
        }
        roots_ai = {
            c.canonical_id for c in visible_pool if c.source_kind == "ai"
        } - roots_uploaded
        pool_facts = {
            str(c.question_id): facts[str(c.question_id)] for c in visible_pool
        }
        items = [
            PracticePreviewItem.model_validate(facts[str(c.question_id)])
            for c in result.selected
        ]
        proof = digest(
            {
                "conditions": selected_conditions.model_dump(mode="json"),
                "pool": pool_facts,
                "selected": [str(c.question_id) for c in result.selected],
                "excluded": excluded,
                "weak": sorted(map(str, weak)),
            }
        )
        view = PracticePreviewView(
            id=row.id,
            subject_id=row.subject_id,
            conditions=selected_conditions,
            preview_digest=proof,
            eligible_count=result.eligible_count,
            uploaded_count=len(roots_uploaded),
            ai_count=len(roots_ai),
            unseen_count=result.unseen_count,
            practiced_count=result.eligible_count - result.unseen_count,
            excluded_count=excluded,
            candidate_shortage=result.candidate_shortage,
            unseen_shortage=result.unseen_shortage,
            selected_count=len(items),
            coverage={str(k): v for k, v in result.coverage.items()},
            weak_knowledge_point_ids=sorted(weak, key=str),
            other_count=result.other_count,
            original_wrong_count=result.original_wrong_count,
            can_start=bool(items) and len(items) == selected_conditions.count,
            items=items,
            generation_operation_id=old.get("generation_operation_id"),
            generation_grounding=old.get("generation_grounding"),
            generation_material_version_ids=old.get(
                "generation_material_version_ids", []
            ),
        )
        row.conditions = {
            **old,
            "request": selected_conditions.model_dump(mode="json"),
            "seed": seed,
            "allowed_ai_ids": sorted(map(str, allowed)),
            "pool": pool_facts,
            "preview": view.model_dump(mode="json"),
        }
        return view

    def preview(
        self, subject_id: UUID, conditions: PracticeConditions
    ) -> PracticePreviewView:
        self.space.get(subject_id)
        with self.sessions.begin() as session:
            session.get(Subject, subject_id, with_for_update=True)
            row = PracticeSession(
                id=uuid4(), subject_id=subject_id, state="preview", conditions={}
            )
            session.add(row)
            view = self._build(session, row, conditions=conditions)
        return view

    def preview_view(self, identity: UUID) -> PracticePreviewView:
        with self.sessions() as session:
            row = self._practice(session, identity)
            if row.state != "preview":
                raise MaterialError("PRACTICE_ALREADY_STARTED", 409)
            return PracticePreviewView.model_validate(
                {
                    **row.conditions["preview"],
                    "generation_grounding": row.conditions.get("generation_grounding"),
                    "generation_material_version_ids": row.conditions.get(
                        "generation_material_version_ids", []
                    ),
                    "generation_operation_id": row.conditions.get(
                        "generation_operation_id"
                    ),
                }
            )

    def _frozen_material_scope(self, session: Session, subject_id: UUID, revision: QuestionRevision) -> dict:
        """Snapshot teaching documents per question; paper filters select origins only."""
        from backend.app.domain.enums import DocumentPurpose, DocumentStatus
        from backend.app.models.document import Document
        from backend.app.models.subject import MaterialSeries, MaterialVersion

        if revision.grounding == "general_knowledge":
            return {"scope_frozen": True, "scope_document_ids": [], "scope_material_version_ids": []}
        evidence = (revision.grounding_refs or {}).get("evidence") or []
        if evidence:
            documents = {UUID(entry["document_id"]) for entry in evidence}
            found = set(session.scalars(select(Document.id).where(
                Document.id.in_(documents), Document.subject_id == subject_id,
                Document.purpose == DocumentPurpose.KNOWLEDGE_BASE)))
            if found != documents:
                raise MaterialError("GRADING_MATERIAL_SCOPE_INVALID", 409)
            versions = {UUID(entry["material_version_id"]) for entry in evidence if entry.get("material_version_id")}
            matched = set(session.scalars(select(MaterialVersion.id).where(
                MaterialVersion.id.in_(versions), MaterialVersion.subject_id == subject_id,
                MaterialVersion.document_id.in_(documents))))
            if matched != versions:
                raise MaterialError("GRADING_MATERIAL_SCOPE_INVALID", 409)
        else:
            documents = set(session.scalars(select(Document.id).where(
                Document.subject_id == subject_id, Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
                Document.status == DocumentStatus.READY)))
            catalogue = list(session.execute(select(MaterialVersion, MaterialSeries.default_version_id)
                .join(MaterialSeries, MaterialSeries.id == MaterialVersion.series_id)
                .where(MaterialVersion.subject_id == subject_id)).all())
            versioned = {v.document_id for v, _default in catalogue}
            defaults = {v.document_id for v, default in catalogue if v.id == default}
            documents &= (documents - versioned) | defaults
            versions = {v.id for v, _default in catalogue if v.document_id in documents}
            # An unversioned document must remain in the frozen document union.
            if documents - versioned:
                versions = set()
        return {"scope_frozen": True, "scope_document_ids": sorted(map(str, documents)),
                "scope_material_version_ids": sorted(map(str, versions))}

    def start(
        self, identity: UUID, body: PracticeStartRequest
    ) -> PersonalPracticeView | PracticePreviewChanged:
        with self.sessions.begin() as session:
            found = self._practice(session, identity)
            session.get(Subject, found.subject_id, with_for_update=True)
            row = self._practice(session, identity, lock=True)
            if row.state != "preview":
                if row.conditions.get(
                    "started_digest"
                ) != body.preview_digest or row.conditions.get("started_scores") != {
                    str(k): str(v) for k, v in body.scores.items()
                }:
                    raise MaterialError("PRACTICE_ALREADY_STARTED", 409)
            else:
                previous = row.conditions
                current = self._build(session, row)
                if current.preview_digest != body.preview_digest:
                    before = previous["pool"]
                    after = row.conditions["pool"]
                    return PracticePreviewChanged(
                        added_question_ids=[
                            UUID(q) for q in after.keys() - before.keys()
                        ],
                        removed_question_ids=[
                            UUID(q) for q in before.keys() - after.keys()
                        ],
                        changed_question_ids=[
                            UUID(q)
                            for q in before.keys() & after.keys()
                            if before[q] != after[q]
                        ],
                        current_preview=current,
                    )
                if not current.can_start:
                    raise MaterialError("INSUFFICIENT_CANDIDATES", 409)
                if not set(body.scores) <= {item.question_id for item in current.items}:
                    raise MaterialError("PRACTICE_SCORE_SCOPE_INVALID", 422)
                for position, selected in enumerate(current.items, 1):
                    revision = session.get(QuestionRevision, selected.revision_id)
                    if revision is None:
                        raise MaterialError("QUESTION_REVISION_NOT_FOUND", 409)
                    content = self.content(revision)
                    fixed = {
                        **content.model_dump(mode="json"),
                        "reference_answer": revision.reference_answer,
                        "scoring_rubric": revision.scoring_rubric,
                        "grounding": revision.grounding,
                        "grounding_refs": revision.grounding_refs,
                        "scope_subject_id": str(row.subject_id),
                        **self._frozen_material_scope(session, row.subject_id, revision),
                        "question_revision_id": str(revision.id),
                        "canonical_id": str(selected.canonical_id),
                        "source_kind": selected.source_kind,
                    }
                    item = PracticeItem(
                        id=uuid4(),
                        session_id=row.id,
                        subject_id=row.subject_id,
                        position=position,
                        question_id=selected.question_id,
                        question_revision_id=selected.revision_id,
                        fixed_max_score=body.scores.get(
                            selected.question_id, selected.default_score
                        ),
                        scoring_input=fixed,
                        knowledge_point_ids=list(
                            map(str, selected.knowledge_point_ids)
                        ),
                    )
                    session.add(item)
                    session.flush()
                    session.add(
                        PracticeAnswer(
                            id=uuid4(),
                            practice_item_id=item.id,
                            answer_state="unanswered",
                            submission_state="draft",
                            choice_ids=None,
                            text_answer=None,
                        )
                    )
                session.flush()
                row.conditions = {
                    **row.conditions,
                    "started_digest": body.preview_digest,
                    "started_scores": {str(k): str(v) for k, v in body.scores.items()},
                }
                row.state = "in_progress"
        return self.practice(identity)

    def practice(self, identity: UUID) -> PersonalPracticeView:
        with self.sessions() as session:
            row = self._practice(session, identity)
            if row.state == "preview":
                raise MaterialError("PRACTICE_NOT_STARTED", 409)
            items = list(
                session.scalars(
                    select(PracticeItem)
                    .where(PracticeItem.session_id == identity)
                    .order_by(PracticeItem.position)
                )
            )
            views = []
            if self.space.profile is None:
                raise MaterialError("LEARNING_ENVIRONMENT_MISMATCH", 409)
            access = PersonalContentAccess(session, self.space.profile)
            for item in items:
                answer = session.scalar(
                    select(PracticeAnswer).where(
                        PracticeAnswer.practice_item_id == item.id
                    )
                )
                if answer is None:
                    raise MaterialError("PRACTICE_ANSWER_NOT_FOUND", 409)
                views.append(
                    PracticeItemView(
                        id=item.id,
                        position=item.position,
                        canonical_id=UUID(item.scoring_input["canonical_id"]),
                        score=item.fixed_max_score,
                        knowledge_point_ids=list(map(UUID, item.knowledge_point_ids)),
                        source_kind=item.scoring_input["source_kind"],
                        grounding=item.scoring_input["grounding"],
                        content=access.view(row.subject_id, row.id, item.id),
                        answer=PracticeAnswerView.model_validate(
                            {
                                "item_id": item.id,
                                "answer_state": answer.answer_state,
                                "choice_ids": answer.choice_ids,
                                "text_answer": answer.text_answer,
                            }
                        ),
                    )
                )
            return PersonalPracticeView(
                id=row.id, subject_id=row.subject_id, state=row.state, items=views
            )

    def save_answer(
        self, identity: UUID, item_id: UUID, body: PracticeAnswerInput
    ) -> PracticeAnswerView:
        with self.sessions.begin() as session:
            row = self._practice(session, identity, lock=True)
            item = session.get(PracticeItem, item_id)
            if (
                item is None
                or item.session_id != identity
                or item.subject_id != row.subject_id
            ):
                raise MaterialError("PRACTICE_ITEM_NOT_FOUND", 404)
            if row.state != "in_progress":
                raise MaterialError("PRACTICE_ANSWER_FROZEN", 409)
            revision = session.get(QuestionRevision, item.question_revision_id)
            if revision is None:
                raise MaterialError("QUESTION_REVISION_NOT_FOUND", 409)
            content = self.content(revision)
            choice_ids = body.choice_ids or None
            text = (
                body.text_answer
                if body.text_answer and body.text_answer.strip()
                else None
            )
            if content.question_type == "short_answer":
                if body.choice_ids is not None:
                    raise MaterialError("ANSWER_INPUT_INVALID", 422)
            elif body.text_answer is not None or (
                choice_ids
                and (
                    not set(choice_ids) <= {option.id for option in content.options}
                    or content.question_type == "single_choice"
                    and len(choice_ids) != 1
                )
            ):
                raise MaterialError("ANSWER_INPUT_INVALID", 422)
            answer = session.scalar(
                select(PracticeAnswer)
                .where(PracticeAnswer.practice_item_id == item_id)
                .with_for_update()
            )
            if answer is None:
                raise MaterialError("PRACTICE_ANSWER_NOT_FOUND", 409)
            answer.choice_ids = choice_ids
            answer.text_answer = text
            answer.answer_state = "answered" if choice_ids or text else "unanswered"
            return PracticeAnswerView.model_validate(
                {
                    "item_id": item_id,
                    "answer_state": answer.answer_state,
                    "choice_ids": choice_ids,
                    "text_answer": text,
                }
            )

    # ---------------------------------------------------------------- grading
    def submit(
        self, identity: UUID, body: PracticeSubmitRequest
    ) -> PracticeReport:
        """整组提交：只评分一次，然后冻结作答与状态。

        重复提交不会产生新轮次或新分数；空白题先报 `PRACTICE_BLANK_ANSWERS`，
        用户确认后本地记零分且不调用模型。
        """

        with self.sessions.begin() as session:
            row = self._practice(session, identity, lock=True)
            if row.state == "preview":
                raise MaterialError("PRACTICE_NOT_STARTED", 409)
            grade = self._grade(
                session, identity, blank_confirmed=body.blank_confirmed
            )
            now = datetime.now(UTC)
            for fact in grade.facts:
                answer = session.get(PracticeAnswer, fact.answer_id)
                if answer is not None and answer.submission_state != "submitted":
                    answer.submission_state = "submitted"
                    answer.submitted_at = now
            row.state = "completed" if grade.all_valid else "partial_failed"
            if row.submitted_at is None:
                row.submitted_at = now
            return self._report(session, row, grade)

    def report(self, identity: UUID) -> PracticeReport:
        """返回最新轮次的真实结果；尚未评分时显式报错。"""

        with self.sessions() as session:
            row = self._practice(session, identity)
            facts = stored_facts(session, identity)
            if not facts:
                raise MaterialError("PRACTICE_NOT_GRADED", 409)
            round_number = max(
                session.scalar(
                    select(GradeAttempt.round)
                    .where(GradeAttempt.answer_id == fact.answer_id)
                    .order_by(GradeAttempt.round.desc())
                )
                or 0
                for fact in facts
            )
            grade = PracticeGrade(round=round_number, facts=facts)
            return self._report(session, row, grade)

    def _report(
        self, session: Session, practice: PracticeSession, grade: PracticeGrade
    ) -> PracticeReport:
        """方案 A 报告：完成率、含未答零的得分率、有效选择题正确率。"""

        items = {
            item.id: item
            for item in session.scalars(
                select(PracticeItem).where(PracticeItem.session_id == practice.id)
            )
        }
        answers = {
            answer.practice_item_id: answer
            for answer in session.scalars(
                select(PracticeAnswer).where(
                    PracticeAnswer.practice_item_id.in_(list(items))
                )
            )
        }
        if self.space.profile is None:
            raise MaterialError("LEARNING_ENVIRONMENT_MISMATCH", 409)
        access = PersonalContentAccess(session, self.space.profile)
        attempts = list(
            session.scalars(
                select(GradeAttempt)
                .where(
                    GradeAttempt.answer_id.in_(
                        [answer.id for answer in answers.values()]
                    )
                )
                .order_by(GradeAttempt.answer_id, GradeAttempt.round)
            )
        )
        history_by_answer: dict[UUID, list[GradeAttempt]] = {}
        for attempt in attempts:
            history_by_answer.setdefault(attempt.answer_id, []).append(attempt)
        disputed = set(
            session.scalars(
                select(ScoreDispute.answer_id).where(
                    ScoreDispute.answer_id.in_(
                        [answer.id for answer in answers.values()]
                    ),
                    ScoreDispute.state != "resolved",
                )
            )
        )
        results: list[PracticeItemResult] = []
        answered = 0
        valid = 0
        answered_zero = 0
        group_points: list[Decimal] = []
        choice_total = 0
        choice_correct = 0
        for fact in sorted(grade.facts, key=lambda entry: entry.position):
            answer = answers.get(fact.item_id)
            state: Literal["answered", "unanswered"] = (
                "answered" if answer and answer.answer_state == "answered" else "unanswered"
            )
            if state == "answered":
                answered += 1
            if fact.validation_state == "valid":
                valid += 1
            is_disputed = bool(answer and answer.id in disputed)
            effective = fact.validation_state == "valid" and not is_disputed
            dispute_id = (
                session.scalar(
                    select(ScoreDispute.id)
                    .where(
                        ScoreDispute.answer_id == answer.id,
                        ScoreDispute.state != "resolved",
                    )
                    .order_by(ScoreDispute.created_at.desc(), ScoreDispute.id.desc())
                    .limit(1)
                )
                if is_disputed and answer is not None
                else None
            )
            if (
                state == "answered"
                and fact.validation_state == "valid"
                and fact.score == ZERO
            ):
                answered_zero += 1
            if fact.validation_state == "valid":
                maximum = fact.max_score or ONE
                if effective:
                    group_points.append((fact.score or ZERO) / maximum)
                if state == "answered" and effective and fact.question_type in {"single_choice", "multiple_choice"}:
                    choice_total += 1
                    if (fact.score or ZERO) == maximum:
                        choice_correct += 1
            results.append(
                PracticeItemResult(
                    item_id=fact.item_id,
                    position=fact.position,
                    question_type=fact.question_type,
                    answer_state=state,
                    score=fact.score,
                    max_score=fact.max_score,
                    validation_state=fact.validation_state,
                    execution_source=fact.execution_source,
                    reason=fact.reason,
                    error_code=fact.error_code,
                    disputed=is_disputed,
                    dispute_id=dispute_id,
                    latest_attempt_id=(
                        history_by_answer[answer.id][-1].id
                        if answer is not None
                        and history_by_answer.get(answer.id)
                        else None
                    ),
                    content=access.view(
                        practice.subject_id, practice.id, fact.item_id
                    ),
                    reference_answer=str(
                        (fact.scoring_input or items[fact.item_id].scoring_input or {}).get(
                            "reference_answer"
                        )
                        or ""
                    ),
                    scoring_rubric=(
                        (fact.scoring_input or items[fact.item_id].scoring_input or {}).get("scoring_rubric")
                    ),
                    answer_text=answer.text_answer if answer else None,
                    choice_ids=answer.choice_ids if answer else None,
                    feedback=_feedback_view(fact.feedback),
                    history=_attempt_history(
                        history_by_answer.get(answer.id, ())
                        if answer is not None
                        else ()
                    ),
                )
            )
        total = len(results)
        all_valid = valid == total
        return PracticeReport(
            practice_id=practice.id,
            subject_id=practice.subject_id,
            state="completed" if all_valid else "partial_failed",
            round=grade.round,
            total_count=total,
            answered_count=answered,
            unanswered_count=total - answered,
            valid_count=valid,
            invalid_count=total - valid,
            answered_zero_count=answered_zero,
            completion_rate=_rate(answered, total),
            group_score_rate=(
                _mean(group_points)
                if all_valid and not disputed and group_points
                else None
            ),
            choice_correct_rate=(
                _rate(choice_correct, choice_total) if choice_total else None
            ),
            choice_sample_count=choice_total,
            items=results,
            unresolved_dispute_count=len(disputed),
            blank_item_ids=[
                fact.item_id
                for fact in grade.facts
                if answers.get(fact.item_id) is None
                or answers[fact.item_id].answer_state != "answered"
            ],
        )


def _feedback_view(value: dict | None) -> PracticeScoringFeedback | None:
    if not value:
        return None
    return PracticeScoringFeedback.model_validate(value)


def _attempt_history(rows) -> list[PracticeAttemptView]:
    return [
        PracticeAttemptView(
            id=row.id,
            round=row.round,
            score=row.score,
            max_score=row.max_score,
            validation_state=row.validation_state,
            execution_source=row.execution_source,
            reason=row.reason,
            error_code=row.error_code,
            confidence=row.confidence,
            runtime_snapshot_id=row.runtime_snapshot_id,
            feedback=_feedback_view(row.feedback),
            created_at=row.created_at,
            question_revision_id=(row.scoring_input or {}).get("question_revision_id"),
            reference_answer=(row.scoring_input or {}).get("reference_answer"),
            scoring_rubric=(row.scoring_input or {}).get("scoring_rubric"),
        )
        for row in rows
    ]


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return ZERO
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        RATE_QUANTUM, rounding=ROUND_HALF_UP
    )


def _mean(values: list[Decimal]) -> Decimal:
    return (sum(values, ZERO) / Decimal(len(values))).quantize(
        RATE_QUANTUM, rounding=ROUND_HALF_UP
    )
