"""Bounded read-only context using existing strict personal retrieval predicates."""

import json
from uuid import UUID

from sqlalchemy import select

from backend.app.ai.retrieval.base import RetrievalFilters, RetrievalQuery
from backend.app.ai.retrieval.keyword_search import KeywordSearchRetriever
from backend.app.domain.enums import DocumentPurpose, DocumentStatus
from backend.app.models.conversation import Message
from backend.app.models.document import Document
from backend.app.models.practice import GradeAttempt, PracticeAnswer, PracticeItem
from backend.app.models.question_revision import QuestionRevision
from backend.app.models.subject import (
    KnowledgePoint,
    MaterialSeries,
    MaterialVersion,
    Subject,
)
from backend.app.services.grading.grading_context import (
    MAX_FINAL_CONTEXT_CHARS as CONTEXT_CHARACTERS,
)
from backend.app.services.grading.grading_context import (
    PER_CHUNK_CHARS as CHUNK_CHARACTERS,
)
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.personal_learning_facts import observations, summarize


def validate_reply(reply, context):
    allowed = {source["id"] for source in context["sources"]}
    if (
        not set(reply.citations) <= allowed
        or len(set(reply.citations)) != len(reply.citations)
        or reply.basis in {"uploaded_material", "mixed"}
        and not reply.citations
        or reply.basis == "model_knowledge"
        and reply.citations
    ):
        raise ValueError("TEACHER_CITATION_INVALID")
    points = {UUID(point["id"]) for point in context["knowledge_points"]}
    if not set(reply.practice_knowledge_point_ids) <= points:
        raise ValueError("TEACHER_PRACTICE_SCOPE_INVALID")
    return reply


def build_context(session, conversation, body):
    subject_id = conversation.subject_id
    subject = session.get(Subject, subject_id)
    rows = observations(session, subject_id)
    stats = summarize(rows)
    stats = stats.model_copy(update={"status": "observed" if stats.valid_count else "insufficient_evidence"})
    context = {
        "subject_id": str(subject_id),
        "subject_name": subject.name,
        "grounding": body.grounding,
        "retrieval_mode": body.retrieval_mode,
        "sources": [],
        "history": [],
        "knowledge_points": [],
        "learning_summary": stats.model_dump(mode="json"),
        "question": None,
        "omitted_context": False,
    }
    points = list(
        session.scalars(
            select(KnowledgePoint)
            .where(
                KnowledgePoint.subject_id == subject_id,
                KnowledgePoint.confirmation_state == "confirmed",
            )
            .order_by(KnowledgePoint.id)
        )
    )
    documents = ()
    versions = tuple(body.material_version_ids)
    if body.practice_item_id:
        item = session.get(PracticeItem, body.practice_item_id)
        if item is None or item.subject_id != subject_id:
            raise MaterialError("TEACHER_QUESTION_SCOPE_INVALID", 404)
        answer = session.scalar(
            select(PracticeAnswer).where(PracticeAnswer.practice_item_id == item.id)
        )
        if answer is None or answer.submission_state != "submitted":
            raise MaterialError("PRACTICE_NOT_SUBMITTED", 409)
        attempt = session.scalar(
            select(GradeAttempt)
            .where(GradeAttempt.answer_id == answer.id)
            .order_by(GradeAttempt.round.desc())
            .limit(1)
        )
        fixed = item.scoring_input
        documents = tuple(UUID(v) for v in fixed.get("scope_document_ids", []))
        frozen_versions = tuple(
            UUID(v) for v in fixed.get("scope_material_version_ids", [])
        )
        if versions and not set(versions) <= set(frozen_versions):
            raise MaterialError("TEACHER_MATERIAL_SCOPE_INVALID", 409)
        versions = versions or frozen_versions
        revision = session.get(QuestionRevision, item.question_revision_id)
        evidence = next((r for r in rows if r.answer_id == answer.id), None)
        context["question"] = {
            "item_id": str(item.id),
            "practice_id": str(item.session_id),
            "revision_id": str(item.question_revision_id),
            "stem_blocks": fixed.get("stem_blocks", revision.stem_blocks),
            "options": fixed.get("options", revision.options),
            "reference_answer": fixed.get("reference_answer"),
            "scoring_rubric": fixed.get("scoring_rubric"),
            "answer_state": answer.answer_state,
            "user_answer": answer.text_answer or answer.choice_ids,
            "grade": None
            if attempt is None
            else {
                "attempt_id": str(attempt.id),
                "round": attempt.round,
                "score": str(attempt.score) if attempt.score is not None else None,
                "max_score": str(attempt.max_score),
                "validation_state": attempt.validation_state,
                "reason": attempt.reason,
                "scoring_input": attempt.scoring_input,
                "diagnosis_exclusion": evidence.exclusion if evidence else "ungraded",
            },
            "visual_input_available": False,
        }
        # Historical grading is provided as read-only evidence, never an invitation
        # to replace its fixed reference/rubric with the current question revision.
    elif not versions and body.grounding == "uploaded_material":
        current = set(
            session.scalars(
                select(Document.id).where(
                    Document.subject_id == subject_id,
                    Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
                    Document.status == DocumentStatus.READY,
                )
            )
        )
        catalogue = list(
            session.execute(
                select(MaterialVersion, MaterialSeries.default_version_id)
                .join(MaterialSeries, MaterialSeries.id == MaterialVersion.series_id)
                .where(MaterialVersion.subject_id == subject_id)
            ).all()
        )
        versioned = {v.document_id for v, _ in catalogue}
        defaults = {v.document_id for v, default in catalogue if v.id == default}
        documents = tuple(sorted(current & ((current - versioned) | defaults), key=str))
    if versions:
        selected = list(
            session.scalars(
                select(MaterialVersion).where(
                    MaterialVersion.subject_id == subject_id,
                    MaterialVersion.id.in_(versions),
                )
            )
        )
        if {v.id for v in selected} != set(versions):
            raise MaterialError("TEACHER_MATERIAL_SCOPE_INVALID", 404)
        selected_documents = tuple(v.document_id for v in selected)
        if documents and not set(selected_documents) <= set(documents):
            raise MaterialError("TEACHER_MATERIAL_SCOPE_INVALID", 409)
        documents = selected_documents
    if body.knowledge_base_ids:
        found = set(
            session.scalars(
                select(Document.knowledge_base_id).where(
                    Document.subject_id == subject_id,
                    Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
                    Document.knowledge_base_id.in_(body.knowledge_base_ids),
                )
            )
        )
        if found != set(body.knowledge_base_ids):
            raise MaterialError("TEACHER_KNOWLEDGE_SCOPE_INVALID", 404)
    candidates = []
    context["scope"] = {
        "subject_id": str(subject_id),
        "document_ids": sorted(map(str, documents)) if body.grounding == "uploaded_material" else [],
        "material_version_ids": sorted(map(str, versions)) if body.grounding == "uploaded_material" else [],
        "knowledge_base_ids": sorted(map(str, body.knowledge_base_ids)),
    }
    if body.grounding == "uploaded_material" and (documents or versions):
        # Explicit keyword-only operation is advertised in preview/UI; it does not
        # resolve unused Embedding/Rerank, silently fallback, or change settings.
        candidates = KeywordSearchRetriever().search(
            session,
            RetrievalQuery(text=body.text, embedding=()),
            filters=RetrievalFilters(
                subject_ids=(subject_id,),
                document_ids=documents,
                material_version_ids=versions,
                knowledge_base_ids=tuple(body.knowledge_base_ids),
            ),
        )
    version_map = {
        v.document_id: v.id
        for v in session.scalars(
            select(MaterialVersion).where(MaterialVersion.subject_id == subject_id)
        )
    }
    for chunk in candidates:
        location = chunk.metadata.get("location")
        if isinstance(location, str):
            location = {"anchor": location}
        if not isinstance(location, dict) or not (
            location.get("page") or location.get("anchor")
        ):
            continue
        document = session.get(Document, UUID(str(chunk.document_id)))
        context["sources"].append(
            {
                "id": str(chunk.chunk_id),
                "document_id": str(chunk.document_id),
                "chunk_id": str(chunk.chunk_id),
                "material_version_id": str(version_map[document.id])
                if document.id in version_map
                else None,
                "location": location,
                "title": document.original_filename,
                "content": chunk.content[:CHUNK_CHARACTERS],
            }
        )
    context["knowledge_points"] = [{"id": str(p.id), "name": p.name,
        "performance": summarize(rows, knowledge_point_id=p.id).model_dump(mode="json")} for p in points]
    # Only completed pairs enter future context. Failed/cancelled/unknown messages
    # never masquerade as the model having successfully answered.
    completed = list(
        session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id, Message.state == "succeeded"
            )
            .order_by(Message.created_at, Message.id)
        )
    )
    context["history"] = [{"role": m.role, "text": m.text} for m in completed]

    def size():
        return len(json.dumps(context, ensure_ascii=False))

    for key in ("history", "sources", "knowledge_points"):
        while size() > CONTEXT_CHARACTERS and context[key]:
            if key == "history":
                del context[key][:2]
            else:
                context[key].pop()
            context["omitted_context"] = True
    if size() > CONTEXT_CHARACTERS:
        # Keep identities and actual status, but explicitly decline to provide
        # partial scoring evidence as if it were complete.
        context["question"] = (
            {
                "item_id": str(body.practice_item_id),
                "context_unavailable": "CONTEXT_LIMIT_EXCEEDED",
            }
            if body.practice_item_id
            else None
        )
        context["omitted_context"] = True
    if size() > CONTEXT_CHARACTERS:
        raise MaterialError("LIMIT_EXCEEDED", 413)
    return context
