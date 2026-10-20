"""Personal question revisions and management; no legacy role or approval state."""
from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.question import Question
from backend.app.models.question_origin import QuestionOrigin
from backend.app.models.question_revision import (
    QuestionRevision,
    question_revision_assets,
    question_revision_knowledge_points,
)
from backend.app.models.subject import KnowledgePoint, Subject
from backend.app.schemas.content_blocks import RevisionContent, SourceLocation
from backend.app.schemas.personal_learning import (
    PersonalQuestionEdit,
    PersonalQuestionView,
)
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.question_identity import canonical_id
from backend.app.services.subject_service import SubjectService


class PersonalQuestionService:
    def __init__(self, space: SubjectService):
        self.space = space
        self.sessions = space.sessions

    def current(self, session: Session, identity: UUID, *, lock: bool = False) -> tuple[Question, QuestionRevision]:
        self.space._check_space()
        question = session.get(Question, identity, with_for_update=lock, populate_existing=lock)
        if question is None or question.subject_id is None or question.current_revision_id is None:
            raise MaterialError("QUESTION_NOT_FOUND", 404)
        revision = session.get(QuestionRevision, question.current_revision_id)
        if revision is None or revision.question_id != question.id:
            raise MaterialError("QUESTION_REVISION_NOT_FOUND", 404)
        return question, revision

    @staticmethod
    def content(revision: QuestionRevision) -> RevisionContent:
        return RevisionContent.model_validate({
            name: getattr(revision, name)
            for name in ("question_type", "stem_blocks", "options", "explanation_blocks")
        })

    def get(self, identity: UUID) -> PersonalQuestionView:
        with self.sessions() as session:
            question, revision = self.current(session, identity)
            origins = list(session.scalars(select(QuestionOrigin).where(
                QuestionOrigin.question_id == identity,
            ).order_by(QuestionOrigin.created_at, QuestionOrigin.id)))
            knowledge = list(session.scalars(select(
                question_revision_knowledge_points.c.knowledge_point_id,
            ).where(question_revision_knowledge_points.c.revision_id == revision.id)))
            locations = [SourceLocation(document_id=o.document_id, **o.source_location)
                         for o in origins if o.document_id is not None]
            refs = revision.grounding_refs
            locations += [SourceLocation.model_validate(loc) for loc in refs.get("source_locations", [])]
            from backend.app.models.question_asset import QuestionAsset
            from backend.app.services.question_asset_access import PersonalContentAccess
            asset_sources = {}
            if self.space.profile is not None:
                access = PersonalContentAccess(session,self.space.profile)
                for asset_id in self.content(revision).asset_ids():
                    asset = session.get(QuestionAsset,asset_id)
                    if asset is None or asset.question_id != identity:
                        raise MaterialError("QUESTION_ASSET_SCOPE_INVALID",409)
                    source = access._asset_source(asset,revision)
                    if source is not None:
                        asset_sources[str(asset_id)] = source
            return PersonalQuestionView(
                **self.content(revision).model_dump(), id=identity,
                subject_id=revision.subject_id, revision_id=revision.id, revision=revision.revision,
                canonical_id=canonical_id(session, identity, revision.subject_id),
                reference_answer=revision.reference_answer, scoring_rubric=revision.scoring_rubric,
                answer_origin=refs.get("answer_origin", next(
                    (o.answer_origin for o in origins if o.answer_origin), None)),
                source_kind="ai" if question.source_type and question.source_type.value == "ai_generated" else "uploaded",
                admission_state=revision.admission_state, default_score=revision.default_score,
                difficulty=revision.difficulty, knowledge_point_ids=knowledge,
                source_locations=locations, asset_sources=asset_sources, issues=refs.get("admission_issues", refs.get("uncertainties", [])),
                uncertainties=refs.get("uncertainties", []),
            )

    def list(self, subject_id: UUID, *, source_kind: str | None = None,
             admission_state: str | None = None, difficulty: str | None = None,
             knowledge_point_ids: list[UUID] | None = None) -> list[PersonalQuestionView]:
        self.space.get(subject_id)
        with self.sessions() as session:
            identities = list(session.scalars(select(Question.id).where(
                Question.subject_id == subject_id,
            ).order_by(Question.created_at, Question.id)))
        rows = [self.get(identity) for identity in identities]
        return [row for row in rows
                if (source_kind is None or row.source_kind == source_kind)
                and (admission_state is None or row.admission_state == admission_state)
                and (difficulty is None or row.difficulty == difficulty)
                and (knowledge_point_ids is None or bool(set(knowledge_point_ids) & set(row.knowledge_point_ids)))]

    def add_revision(self, session: Session, question: Question, old: QuestionRevision,
                     *, content: RevisionContent, reference_answer: str | None,
                     scoring_rubric: str | None, refs: dict, admission_state: str,
                     difficulty: str | None, score=None, knowledge_point_ids=None) -> QuestionRevision:
        revision = QuestionRevision(
            id=uuid4(), question_id=question.id, subject_id=old.subject_id,
            revision=old.revision + 1, **content.model_dump(mode="json"),
            reference_answer=reference_answer, scoring_rubric=scoring_rubric,
            grounding=old.grounding, grounding_refs=deepcopy(refs),
            admission_state=admission_state,
            default_score=old.default_score if score is None else score,
            difficulty=difficulty,
        )
        session.add(revision)
        session.flush()
        from backend.app.models.question_asset import QuestionAsset
        from backend.app.schemas.content_blocks import OriginalBlock
        from backend.app.services.question_asset_access import PersonalContentAccess
        if self.space.profile is not None:
            access = PersonalContentAccess(session,self.space.profile)
            for block in content.blocks():
                if isinstance(block,OriginalBlock):
                    asset = session.get(QuestionAsset,block.asset_id)
                    if asset is None or access._asset_source(asset,old) != block.source:
                        raise MaterialError("QUESTION_ASSET_SOURCE_INVALID",409)
        for asset_id in content.asset_ids():
            if session.scalar(select(question_revision_assets.c.asset_id).where(
                question_revision_assets.c.revision_id == old.id,
                question_revision_assets.c.asset_id == asset_id,
            )) is None:
                raise MaterialError("QUESTION_ASSET_SCOPE_INVALID", 409)
            session.execute(question_revision_assets.insert().values(revision_id=revision.id, asset_id=asset_id))
        kp_ids = knowledge_point_ids
        if kp_ids is None:
            kp_ids = list(session.scalars(select(question_revision_knowledge_points.c.knowledge_point_id).where(
                question_revision_knowledge_points.c.revision_id == old.id,
            )))
        allowed = set(session.scalars(select(KnowledgePoint.id).where(
            KnowledgePoint.subject_id == old.subject_id, KnowledgePoint.confirmation_state == "confirmed",
        )))
        if not set(kp_ids) <= allowed:
            raise MaterialError("KNOWLEDGE_POINT_NOT_FOUND", 404)
        for kp_id in kp_ids:
            session.execute(question_revision_knowledge_points.insert().values(
                revision_id=revision.id, subject_id=old.subject_id, knowledge_point_id=kp_id,
            ))
        question.current_revision_id = revision.id
        return revision

    def revise(self, identity: UUID, body: PersonalQuestionEdit) -> PersonalQuestionView:
        with self.sessions.begin() as session:
            question, old = self.current(session, identity)
            session.get(Subject, old.subject_id, with_for_update=True)
            question, old = self.current(session, identity, lock=True)
            if old.revision != body.expected_revision:
                raise MaterialError("REVISION_CHANGED", 409)
            origins = list(session.scalars(select(QuestionOrigin).where(QuestionOrigin.question_id == identity)))
            allowed = {(o.document_id, o.source_location.get("page"), o.source_location.get("anchor")) for o in origins}
            allowed |= {(UUID(loc["document_id"]),loc.get("page"),loc.get("anchor"))
                        for loc in old.grounding_refs.get("source_locations", [])}
            if any(o.source_kind=="uploaded" for o in origins) and not body.source_locations:
                raise MaterialError("QUESTION_SOURCE_SCOPE_INVALID",409)
            if any((loc.document_id, loc.page, loc.anchor) not in allowed for loc in body.source_locations):
                raise MaterialError("QUESTION_SOURCE_SCOPE_INVALID", 409)
            refs = deepcopy(old.grounding_refs)
            refs.pop("quality_operation_id", None)
            refs.pop("quality", None)
            refs["uncertainties"] = body.uncertainties
            refs["admission_issues"] = []
            if old.reference_answer != body.reference_answer:
                refs["answer_origin"] = "user_correction" if body.reference_answer else None
            refs["confirmed_asset_ids"] = [str(a) for a in body.confirmed_asset_ids]
            self.add_revision(
                session, question, old, content=RevisionContent.model_validate(
                    body.model_dump(include={"question_type", "stem_blocks", "options", "explanation_blocks"})),
                reference_answer=body.reference_answer, scoring_rubric=body.scoring_rubric,
                refs=refs, admission_state="needs_validation" if body.reference_answer else "incomplete",
                score=body.score, difficulty=body.difficulty, knowledge_point_ids=body.knowledge_point_ids,
            )
        return self.get(identity)
