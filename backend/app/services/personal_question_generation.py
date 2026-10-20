"""One explicitly confirmed generation response, with frozen grounding and no fallback."""

import json
from uuid import UUID, uuid4

from sqlalchemy import select

from backend.app.ai.agents.question_agent import personal_generation_messages
from backend.app.ai.retrieval.base import RetrievalFilters, RetrievalQuery
from backend.app.ai.retrieval.keyword_search import KeywordSearchRetriever
from backend.app.domain.enums import QuestionSourceType
from backend.app.models.question import Question
from backend.app.models.question_origin import QuestionOrigin
from backend.app.models.question_revision import (
    QuestionRevision,
    question_revision_knowledge_points,
)
from backend.app.models.subject import KnowledgePoint, MaterialVersion, Subject
from backend.app.schemas.content_blocks import RevisionContent
from backend.app.schemas.personal_practice import (
    GenerationRequest,
    PersonalGenerationResult,
)
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import LearningOperations, StepPlan
from backend.app.services.practice_service import PracticeService
from backend.app.services.question_validator import personal_admission


class PersonalQuestionGeneration(PracticeService):
    def prepare(
        self, identity: UUID, body: GenerationRequest, operations: LearningOperations
    ):
        with self.sessions.begin() as session:
            found = self._practice(session, identity)
            session.get(Subject, found.subject_id, with_for_update=True)
            row = self._practice(session, identity, lock=True)
            if row.state != "preview":
                raise MaterialError("PRACTICE_ALREADY_STARTED", 409)
            preview = self._build(session, row)
            if preview.conditions.source == "uploaded":
                raise MaterialError("AI_SOURCE_CONFIRMATION_REQUIRED", 409)
            if preview.conditions.source_document_ids:
                raise MaterialError("GENERATION_SCOPE_CONFLICT", 409)
            required = preview.conditions.count - preview.selected_count
            if required <= 0:
                raise MaterialError("GENERATION_NOT_NEEDED", 409)
            existing_id = row.conditions.get("generation_operation_id")
            if existing_id:
                existing = operations.view(UUID(existing_id))
                old_payload = json.loads(existing.steps[0]["messages"][-1]["content"])
                same = (
                    old_payload["grounding"] == body.grounding
                    and set(old_payload["material_version_ids"])
                    == set(map(str, body.material_version_ids))
                    and old_payload["preview_digest"] == preview.preview_digest
                )
                if existing.state in {"running", "outcome_unknown"}:
                    raise MaterialError("GENERATION_OUTCOME_UNRESOLVED", 409)
                if same and existing.state in {"prepared", "confirmed"}:
                    return existing
            subject = session.get(Subject, row.subject_id)
            if subject is None:
                raise MaterialError("SUBJECT_NOT_FOUND", 404)
            points = list(
                session.scalars(
                    select(KnowledgePoint).where(
                        KnowledgePoint.subject_id == row.subject_id,
                        KnowledgePoint.confirmation_state == "confirmed",
                    )
                )
            )
            requested = set(preview.conditions.knowledge_point_ids)
            allowed = {
                point.id for point in points if not requested or point.id in requested
            }
            evidence = []
            if body.grounding == "uploaded_material":
                versions = list(
                    session.scalars(
                        select(MaterialVersion).where(
                            MaterialVersion.id.in_(body.material_version_ids),
                            MaterialVersion.subject_id == row.subject_id,
                        )
                    )
                )
                if {version.id for version in versions} != set(
                    body.material_version_ids
                ):
                    raise MaterialError("MATERIAL_VERSION_SCOPE_INVALID", 404)
                # Explicit keyword retrieval: no unused vector or rerank clients/steps.
                # Strict SQL scope/confirmed knowledge predicates are shared with normal retrieval.
                query = (
                    " ".join(point.name for point in points if point.id in allowed)
                    or subject.name
                )
                chunks = KeywordSearchRetriever().search(
                    session,
                    RetrievalQuery(
                        text=query,
                        embedding=(),
                        knowledge_points=tuple(map(str, requested)),
                    ),
                    filters=RetrievalFilters(
                        subject_ids=(row.subject_id,),
                        material_version_ids=tuple(body.material_version_ids),
                    ),
                )
                if not chunks or not any(chunk.content.strip() for chunk in chunks):
                    raise MaterialError("GENERATION_MATERIAL_INSUFFICIENT", 409)
                version_by_document = {
                    version.document_id: version.id for version in versions
                }
                for chunk in chunks:
                    location = chunk.metadata.get("location")
                    if isinstance(location, str) and location.strip():
                        location = {"anchor": location}
                    if not isinstance(location, dict) or not (
                        location.get("page") or location.get("anchor")
                    ):
                        raise MaterialError("GENERATION_MATERIAL_LOCATION_INVALID", 409)
                    evidence.append(
                        {
                            "chunk_id": str(chunk.chunk_id),
                            "document_id": str(chunk.document_id),
                            "material_version_id": str(
                                version_by_document[UUID(chunk.document_id)]
                            ),
                            "location": location,
                            "content": chunk.content,
                        }
                    )
            payload = {
                "practice_id": str(row.id),
                "preview_digest": preview.preview_digest,
                "subject_id": str(row.subject_id),
                "subject_name": subject.name,
                "conditions": preview.conditions.model_dump(mode="json"),
                "count": required,
                "grounding": body.grounding,
                "material_version_ids": list(map(str, body.material_version_ids)),
                "knowledge_points": [
                    {"id": str(point.id), "name": point.name}
                    for point in points
                    if point.id in allowed
                ],
                "evidence": evidence,
                "retrieval_mode": "keyword_only" if evidence else "none",
            }
            messages = personal_generation_messages(
                payload, PersonalGenerationResult.model_json_schema()
            )
            operation = operations.prepare(
                purpose="personal_generation:" + str(identity),
                idempotency_key=body.idempotency_key,
                steps=[
                    StepPlan(
                        step_id="generate",
                        task_kind="question_generation",
                        request_budget=1,
                        messages=messages,
                        scope_summary=(
                            f"科目：{subject.name}；数量：{required}；依据："
                            + (
                                "AI／通用知识，无教材引用"
                                if body.grounding == "general_knowledge"
                                else "所选上传资料，关键词检索"
                            )
                            + "；知识点："
                            + (
                                "、".join(
                                    point.name for point in points if point.id in allowed
                                )
                                or "科目范围"
                            )
                            + "；难度："
                            + (preview.conditions.difficulty or "不限")
                            + "；资料版本："
                            + ("、".join(map(str,body.material_version_ids)) or "无")
                        ),
                    )
                ],
            )
            row.conditions = {
                **row.conditions,
                "generation_operation_id": str(operation.id),
                "generation_grounding": body.grounding,
                "generation_material_version_ids": list(
                    map(str, body.material_version_ids)
                ),
            }
            return operation

    async def execute(
        self, identity: UUID, operation_id: UUID, operations: LearningOperations
    ):
        operation = operations.view(operation_id)
        if operation.purpose != "personal_generation:" + str(identity):
            raise MaterialError("GENERATION_OPERATION_MISMATCH", 409)
        payload = json.loads(operation.steps[0]["messages"][-1]["content"])
        with self.sessions() as session:
            row = self._practice(session, identity)
            if row.state != "preview" or row.conditions.get(
                "generation_operation_id"
            ) != str(operation_id):
                raise MaterialError("GENERATION_OPERATION_MISMATCH", 409)
            if str(operation_id) in row.conditions.get("applied_generation_ids", []):
                return self.preview_view(identity)
            if row.conditions["preview"]["preview_digest"] != payload["preview_digest"]:
                raise MaterialError("PRACTICE_PREVIEW_CHANGED", 409)

        async def invoke(runtime, step):
            lease = runtime.acquire(step.task_kind)
            return await lease.client.generate_structured(
                step.messages,
                PersonalGenerationResult,
                model=runtime.requested_model(step.task_kind),
            )

        result = await operations.execute(operation_id, invoke=invoke)
        if result.state != "succeeded":
            return result
        output = PersonalGenerationResult.model_validate(result.steps[0]["result"])
        if len(output.questions) != payload["count"]:
            raise MaterialError("GENERATION_COUNT_MISMATCH", 409)
        with self.sessions.begin() as session:
            found = self._practice(session, identity)
            session.get(Subject, found.subject_id, with_for_update=True)
            row = self._practice(session, identity, lock=True)
            applied = row.conditions.get("applied_generation_ids", [])
            if str(operation_id) in applied:
                return self.preview_view(identity)
            if (
                row.state != "preview"
                or row.conditions["preview"]["preview_digest"]
                != payload["preview_digest"]
            ):
                raise MaterialError("PRACTICE_PREVIEW_CHANGED", 409)
            allowed_points = {UUID(p["id"]) for p in payload["knowledge_points"]}
            allowed_evidence = {UUID(e["chunk_id"]) for e in payload["evidence"]}
            created = []
            for generated in output.questions:
                if (
                    generated.question_type
                    not in payload["conditions"]["question_types"]
                ):
                    raise MaterialError("GENERATION_TYPE_MISMATCH", 409)
                if set(generated.knowledge_point_ids) - allowed_points or (
                    payload["conditions"]["knowledge_point_ids"]
                    and not generated.knowledge_point_ids
                ):
                    raise MaterialError("GENERATION_KNOWLEDGE_SCOPE_INVALID", 409)
                if (
                    payload["conditions"]["difficulty"] is not None
                    and generated.difficulty != payload["conditions"]["difficulty"]
                ):
                    raise MaterialError("GENERATION_DIFFICULTY_MISMATCH", 409)
                content = RevisionContent.model_validate(
                    generated.model_dump(
                        include={
                            "question_type",
                            "stem_blocks",
                            "options",
                            "explanation_blocks",
                        }
                    )
                )
                # No externally supplied pixels/asset identities exist in this operation.
                if content.asset_ids():
                    raise MaterialError("GENERATION_ASSET_SCOPE_INVALID", 409)
                state, issues = personal_admission(
                    content=content,
                    reference_answer=generated.reference_answer,
                    scoring_rubric=generated.scoring_rubric,
                    score=10,
                    quality=generated.quality,
                    allowed_evidence_ids=allowed_evidence,
                    uncertainties=generated.uncertainties,
                    confirmed_asset_ids=set(),
                )
                used = set(generated.evidence_chunk_ids)
                if not used <= allowed_evidence or (
                    payload["grounding"] == "uploaded_material" and not used
                ):
                    issues = sorted(set(issues + ["GENERATION_EVIDENCE_INVALID"]))
                    state = "needs_correction"
                references = [
                    e for e in payload["evidence"] if UUID(e["chunk_id"]) in used
                ]
                question = Question(
                    id=uuid4(),
                    subject_id=row.subject_id,
                    course_id=None,
                    created_by=None,
                    source_type=QuestionSourceType.AI_GENERATED,
                )
                session.add(question)
                session.flush()
                revision = QuestionRevision(
                    id=uuid4(),
                    question_id=question.id,
                    subject_id=row.subject_id,
                    revision=1,
                    **content.model_dump(mode="json"),
                    reference_answer=generated.reference_answer,
                    scoring_rubric=generated.scoring_rubric,
                    grounding=payload["grounding"],
                    grounding_refs={
                        "answer_origin": "ai_completion",
                        "generation_operation_id": str(operation_id),
                        "runtime_snapshot_id": str(result.runtime_snapshot_id),
                        "quality": generated.quality.model_dump(mode="json"),
                        "uncertainties": generated.uncertainties,
                        "admission_issues": issues,
                        "evidence": references,
                        "source_locations": [
                            {"document_id": e["document_id"], **e["location"]}
                            for e in references
                        ],
                    },
                    admission_state=state,
                    default_score=10,
                    difficulty=generated.difficulty,
                )
                session.add(revision)
                session.flush()
                question.current_revision_id = revision.id
                session.add(
                    QuestionOrigin(
                        id=uuid4(),
                        question_id=question.id,
                        subject_id=row.subject_id,
                        source_kind="ai",
                        document_id=None,
                        generation_run_id=operation_id,
                        source_location={"grounding": payload["grounding"]},
                        answer_origin="ai_completion",
                    )
                )
                for point in set(generated.knowledge_point_ids):
                    session.execute(
                        question_revision_knowledge_points.insert().values(
                            revision_id=revision.id,
                            subject_id=row.subject_id,
                            knowledge_point_id=point,
                        )
                    )
                created.append(question.id)
            allowed = set(map(UUID, row.conditions.get("allowed_ai_ids", []))) | set(
                created
            )
            row.conditions = {
                **row.conditions,
                "applied_generation_ids": [*applied, str(operation_id)],
            }
            view = self._build(session, row, allowed_ai=allowed)
        return view
