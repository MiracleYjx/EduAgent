"""Confirmed personal grading on the existing durable LearningOperations boundary.

The operation commits send reservations independently of GradeAttempt. Stored
results can be applied again without sending; running/unknown operations cannot
be resumed by an HTTP replay. This is deliberately not distributed atomicity.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.practice import GradeAttempt, PracticeAnswer, PracticeItem
from backend.app.models.score_dispute import ScoreDispute
from backend.app.schemas.personal_learning import LearningOperationView
from backend.app.schemas.personal_practice import (
    PracticeGradingPreviewRequest,
    ScoreDisputeRegradeRequest,
)
from backend.app.services.grading.personal_subjective import (
    PersonalSubjectivePort,
    _frozen_content,
    _knowledge_point_names,
)
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import (
    LearningOperations,
    OperationError,
    StepPlan,
    digest,
)
from backend.app.services.personal_grading import (
    BlankAnswersPending,
    SubjectiveResult,
    grade_practice,
    stored_facts,
)
from backend.app.services.score_dispute_service import ScoreDisputeService


class GradingStepResult(BaseModel):
    outcome: SubjectiveResult


class PersonalGradingOperations(ScoreDisputeService):
    def __init__(self, space, operations: LearningOperations):
        super().__init__(space)
        self.operations = operations
        if (
            space.config is None
            or space.profile is None
            or operations.config.environment_id != space.profile.environment_id
        ):
            raise MaterialError("LEARNING_ENVIRONMENT_MISMATCH", 409)

    def _items(
        self,
        session: Session,
        practice_id: UUID,
        dispute_id: UUID | None,
        request: dict[str, Any] | None,
    ):
        practice = self._practice(session, practice_id, lock=True)
        if practice.state == "preview":
            raise MaterialError("PRACTICE_NOT_STARTED", 409)
        if dispute_id is None:
            items = list(
                session.scalars(
                    select(PracticeItem)
                    .where(PracticeItem.session_id == practice_id)
                    .order_by(PracticeItem.position)
                )
            )
        else:
            dispute = session.get(ScoreDispute, dispute_id, with_for_update=True)
            if dispute is None:
                raise MaterialError("DISPUTE_NOT_FOUND", 404)
            if dispute.state == "resolved":
                raise MaterialError("DISPUTE_ALREADY_RESOLVED", 409)
            answer = session.get(PracticeAnswer, dispute.answer_id)
            item = (
                session.get(PracticeItem, answer.practice_item_id) if answer else None
            )
            if answer is None or item is None or item.session_id != practice_id:
                raise MaterialError("PRACTICE_ITEM_NOT_FOUND", 404)
            body = ScoreDisputeRegradeRequest.model_validate(request)
            latest = self._latest(session, answer.id)
            if latest.validation_state == "outcome_unknown":
                raise OperationError("UNKNOWN_OUTCOME_ACK_REQUIRED")
            if (
                body.expected_attempt_id is None
                or latest.id != body.expected_attempt_id
            ):
                raise MaterialError("DISPUTE_ROUND_STALE", 409)
            facts = {"dispute_id": str(dispute_id), **body.model_dump(mode="json")}
            items = [self.grading_item(session, item, latest, body, facts)]
        answers = {
            a.practice_item_id: a
            for a in session.scalars(
                select(PracticeAnswer).where(
                    PracticeAnswer.practice_item_id.in_([i.id for i in items])
                )
            )
        }
        if len(answers) != len(items):
            raise MaterialError("PRACTICE_ANSWER_NOT_FOUND", 409)
        return practice, items, answers

    def _scope(self, session, items, answers):
        result = []
        for item in items:
            frozen = dict(item.scoring_input)
            # A new operation records the actual point names, never later names.
            frozen.pop("operation_id", None)
            frozen["scope_knowledge_point_names"] = list(
                _knowledge_point_names(session, item)
            )
            answer = answers[item.id]
            result.append(
                {
                    "item_id": str(item.id),
                    "session_id": str(item.session_id),
                    "subject_id": str(item.subject_id),
                    "question_id": str(item.question_id),
                    "question_revision_id": str(item.question_revision_id),
                    "position": item.position,
                    "fixed_max_score": str(item.fixed_max_score),
                    "knowledge_point_ids": list(item.knowledge_point_ids),
                    "scoring_input": frozen,
                    "answer": {
                        "id": str(answer.id),
                        "answer_state": answer.answer_state,
                        "choice_ids": answer.choice_ids,
                        "text_answer": answer.text_answer,
                    },
                }
            )
        return result

    def prepare(
        self,
        practice_id: UUID,
        body: PracticeGradingPreviewRequest,
        *,
        dispute_id: UUID | None = None,
        regrade: ScoreDisputeRegradeRequest | None = None,
    ) -> LearningOperationView:
        revision = self.operations.config.active_revision()
        if revision is None:
            raise OperationError("OPERATION_DEPENDENCY_UNAVAILABLE")
        request = regrade.model_dump(mode="json") if regrade is not None else None
        with self.sessions.begin() as session:
            practice, items, answers = self._items(
                session, practice_id, dispute_id, request
            )
            key = str(dispute_id) if dispute_id else "submit"
            previous = practice.conditions.get("grading_executions", {}).get(key)
            if previous and not self._applied(session, UUID(previous)):
                raise OperationError("GRADING_OPERATION_IN_PROGRESS")
            if dispute_id is None and stored_facts(session, practice_id):
                raise MaterialError("PRACTICE_ALREADY_GRADED", 409)
            blanks = [
                i.id
                for i in items
                if answers[i.id].answer_state != "answered"
                and i.scoring_input.get("question_type") == "short_answer"
            ]
            if blanks and not body.blank_confirmed:
                raise BlankAnswersPending(blanks)
            scope = self._scope(session, items, answers)
            scope_digest = digest(scope)
            steps = []
            for item, frozen in zip(items, scope, strict=True):
                if (
                    item.scoring_input.get("question_type") != "short_answer"
                    or answers[item.id].answer_state != "answered"
                ):
                    continue
                content = _frozen_content(item.scoring_input)
                primary = (
                    "vision"
                    if content.asset_ids(include_explanation=False)
                    else "subjective_grading"
                )
                tasks = {primary: 1}
                if primary == "vision":
                    # The grading binding still owns the retrieval mode.
                    tasks["subjective_grading"] = 0
                values = revision.values
                if item.scoring_input.get("grounding") == "uploaded_material":
                    binding = values.bindings.get("subjective_grading")
                    default = values.bindings.get("default_llm")
                    mode = (
                        getattr(binding, "retrieval_mode", None)
                        or getattr(default, "retrieval_mode", None)
                        or "hybrid_rerank"
                    )
                    if mode in {"semantic", "hybrid", "hybrid_rerank"}:
                        tasks["embedding"] = 1
                    if mode == "hybrid_rerank":
                        tasks["rerank"] = 1
                for task in tasks:
                    resolved = self.operations.config.provider_registry.resolve_binding(
                        task_kind=task,
                        profiles=values.providers,
                        bindings=values.bindings,
                    )
                    profile = values.providers[str(resolved.profile_id)]
                    if profile.adapter_id in {
                        "local",
                        "bge",
                        "huggingface",
                        "cross_encoder",
                    }:
                        tasks[task] = 0
                data = {
                    "practice_id": str(practice_id),
                    "dispute_id": str(dispute_id) if dispute_id else None,
                    "regrade_request": request,
                    "scope_digest": scope_digest,
                    "settings_revision": str(revision.revision_id),
                    "item": frozen,
                }
                steps.append(
                    StepPlan(
                        step_id=str(item.id),
                        task_kind=primary,
                        messages=[],
                        grading_input=data,
                        task_budgets=tasks,
                        request_budget=sum(tasks.values()),
                        scope_summary=f"第 {item.position} 题；固定题目、作答、参考答案、评分标准及资料范围。"
                        + ("采用本次明确选定的题库修订：" if regrade and regrade.revision_id else "沿用冻结修订：")
                        + str(item.question_revision_id) + "。"
                        + "；".join(f"{self._task_label(task)}最多 {budget} 次远程请求" for task, budget in tasks.items() if budget)
                        + "。费用无法可靠估算，非账单硬上限。",
                    )
                )
            if not steps:
                raise OperationError("GRADING_OPERATION_NOT_REQUIRED")
            operation = self.operations.prepare(
                purpose=f"{'dispute_regrade' if dispute_id else 'practice_grading'}:{dispute_id or practice_id}",
                idempotency_key=body.idempotency_key,
                steps=steps,
                storage_root=self.space.storage_root,
                expected_settings_revision=revision.revision_id,
            )
            practice.conditions = {
                **practice.conditions,
                "grading_previews": {
                    **practice.conditions.get("grading_previews", {}),
                    key: str(operation.id),
                },
            }
            return operation

    @staticmethod
    def _task_label(task: str) -> str:
        return {"subjective_grading":"主观评分", "vision":"题图评分",
                "embedding":"教材向量检索", "rerank":"教材重排"}[task]

    def _data(self, operation_id: UUID) -> tuple[LearningOperationView, dict[str, Any]]:
        operation = self.operations.view(operation_id)
        data = operation.steps[0].get("grading_input") if operation.steps else None
        if not isinstance(data, dict) or operation.purpose != (
            f"dispute_regrade:{data['dispute_id']}"
            if data.get("dispute_id")
            else f"practice_grading:{data['practice_id']}"
        ):
            raise OperationError("GRADING_OPERATION_MISMATCH")
        return operation, data

    def _validate(self, session: Session, data):
        current = self.operations.config.active_revision()
        if current is None or str(current.revision_id) != data["settings_revision"]:
            raise OperationError("PREVIEW_CHANGED")
        practice, items, answers = self._items(
            session,
            UUID(data["practice_id"]),
            UUID(data["dispute_id"]) if data["dispute_id"] else None,
            data["regrade_request"],
        )
        if digest(self._scope(session, items, answers)) != data["scope_digest"]:
            raise OperationError("PREVIEW_CHANGED")
        return practice, items, answers

    def confirm(self, operation_id: UUID, preview_digest: str):
        operation, data = self._data(operation_id)
        if operation.state in {"prepared", "confirmed"}:
            with self.sessions.begin() as session:
                self._validate(session, data)
                return self.operations.confirm(
                    operation_id, preview_digest=preview_digest
                )
        return self.operations.confirm(operation_id, preview_digest=preview_digest)

    @staticmethod
    def _restore(data):
        item = data["item"]
        fields = {k: v for k, v in item.items() if k != "answer"}
        fields["id"] = fields.pop("item_id")
        for k in (
            "id",
            "session_id",
            "subject_id",
            "question_id",
            "question_revision_id",
        ):
            fields[k] = UUID(fields[k])
        fields["fixed_max_score"] = Decimal(fields["fixed_max_score"])
        restored = PracticeItem(**fields)
        answer = item["answer"]
        return restored, PracticeAnswer(
            id=UUID(answer["id"]),
            practice_item_id=restored.id,
            answer_state=answer["answer_state"],
            submission_state="submitted",
            choice_ids=answer["choice_ids"],
            text_answer=answer["text_answer"],
        )

    def execute(
        self,
        operation_id: UUID,
        *,
        practice_id: UUID | None = None,
        dispute_id: UUID | None = None,
    ):
        operation, data = self._data(operation_id)
        if (
            str(practice_id or data["practice_id"]) != data["practice_id"]
            or (str(dispute_id) if dispute_id else None) != data["dispute_id"]
        ):
            raise OperationError("GRADING_OPERATION_MISMATCH")
        identity = UUID(data["practice_id"])
        key = data["dispute_id"] or "submit"
        with self.sessions.begin() as session:
            practice = self._practice(session, identity, lock=True)
            applied = self._applied(session, operation_id)
            if applied:
                return self.view(dispute_id) if dispute_id else self.report(identity)
            if operation.state == "prepared":
                raise OperationError("CONFIRMATION_REQUIRED")
            if operation.state == "running":
                return operation
            bindings = practice.conditions.get("grading_executions", {})
            previous = bindings.get(key)
            if previous and previous != str(operation_id):
                old = self.operations.view(UUID(previous))
                if old.state in {"confirmed", "running"} or not self._applied(
                    session, UUID(previous)
                ):
                    raise OperationError("GRADING_OPERATION_IN_PROGRESS")
            if operation.state == "confirmed":
                practice, _items, answers = self._validate(session, data)
                self.operations.preflight(operation_id)
                if not dispute_id and stored_facts(session, identity):
                    raise OperationError("PRACTICE_ALREADY_GRADED")
                practice.conditions = {
                    **practice.conditions,
                    "grading_executions": {**bindings, key: str(operation_id)},
                }
                if not dispute_id:
                    practice.state = "submitted"
                    now = datetime.now(UTC)
                    practice.submitted_at = practice.submitted_at or now
                    for answer in answers.values():
                        if answer.submission_state != "submitted":
                            answer.submission_state = "submitted"
                            answer.submitted_at = now
            elif previous != str(operation_id):
                raise OperationError("GRADING_OPERATION_MISMATCH")
        # Separate transaction: durable sends survive cancellation/grade rollback.
        with self.sessions.begin() as session:
            practice = self._practice(session, identity, lock=True)
            if self._applied(session, operation_id):
                return self.view(dispute_id) if dispute_id else self.report(identity)

            async def invoke(runtime, step):
                item, answer = self._restore(step.grading_input)
                outcome = await PersonalSubjectivePort(
                    session=session, runtime=runtime, profile=self.space.profile
                ).grade(item, answer)
                if outcome.validation_state == "failed":
                    raise OperationError(
                        outcome.error_code or "SUBJECTIVE_GRADING_FAILED"
                    )
                return GradingStepResult(outcome=outcome)

            operation = asyncio.run(
                self.operations.execute(operation_id, invoke=invoke)
            )
            if operation.state in {"prepared", "confirmed", "running"}:
                return operation
            grading_items = {}
            outcomes = {}
            for step in operation.steps:
                item, _answer = self._restore(step["grading_input"])
                item.scoring_input = {
                    **item.scoring_input,
                    "operation_id": str(operation_id),
                }
                grading_items[item.id] = item
                if step["state"] == "succeeded" and step.get("result"):
                    outcome = GradingStepResult.model_validate(step["result"]).outcome
                else:
                    unknown = step["state"] == "outcome_unknown"
                    outcome = SubjectiveResult(
                        score=None,
                        validation_state="outcome_unknown" if unknown else "failed",
                        reason="评分结果未知，请人工核对，不会自动补发。"
                        if unknown
                        else "评分未完成，未生成分数。",
                        error_code=step.get("error_code") or "OPERATION_STOPPED",
                        runtime_snapshot_id=operation.runtime_snapshot_id,
                    )
                outcomes[item.id] = replace(
                    outcome,
                    usage={
                        "operation_id": str(operation_id),
                        "actual_requests": step["actual_requests"],
                        "request_budget": step["request_budget"],
                        "requests": step.get("requests", []),
                    },
                )
            grade = grade_practice(
                session,
                practice_id=identity,
                blank_confirmed=True,
                regrade=bool(dispute_id),
                item_ids=tuple(grading_items) if dispute_id else None,
                grading_items=grading_items,
                subjective=lambda item, _answer: outcomes[item.id],
            )
            if dispute_id:
                dispute = session.get(ScoreDispute, dispute_id, with_for_update=True)
                if dispute is None:
                    raise MaterialError("DISPUTE_NOT_FOUND", 404)
                dispute.state = "awaiting_acceptance" if grade.all_valid else "pending"
            else:
                practice.state = "completed" if grade.all_valid else "partial_failed"
        return self.view(dispute_id) if dispute_id else self.report(identity)

    def _applied(self, session: Session, operation_id: UUID) -> bool:
        return (
            session.scalar(
                select(GradeAttempt.id)
                .where(
                    GradeAttempt.scoring_input["operation_id"].as_string()
                    == str(operation_id)
                )
                .limit(1)
            )
            is not None
        )
