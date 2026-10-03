"""T165 adaptation source boundary: authorized snapshot, locked graph and atomic links."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Course, Question, QuestionSourcePaper
from backend.app.schemas.paper_import import PixelRegion
from backend.app.schemas.question_assets import AssetLinkRequest
from backend.app.services.file_storage_service import StoredFile
from backend.app.services.question_asset_service import QuestionAssetService

AdaptationType = Literal["rewrite", "translate", "extend"]


class QuestionAdaptationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.error_code, self.detail = code, message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class AdaptationSnapshot:
    source_question_id: UUID
    course_id: UUID
    adaptation_type: AdaptationType
    context: dict[str, Any]


def _context(question: Question) -> dict[str, Any]:
    return {
        "source_question_id": str(question.id),
        "course_id": str(question.course_id),
        "content": question.content,
        "options": deepcopy(question.options),
        "reference_answer": question.reference_answer,
        "analysis": question.analysis,
        "scoring_rubric": question.scoring_rubric,
        "question_type": question.type.value,
        "score": str(question.score),
        "difficulty": question.difficulty,
        "knowledge_points": list(question.knowledge_points),
        "validation_revision": question.validation_revision,
        "order_preserved": question.order_preserved,
        "assets": [
            {
                "id": str(asset.id),
                "asset_type": asset.asset_type,
                "file_id": asset.file_id,
                "source_page_id": str(asset.source_page_id)
                if asset.source_page_id
                else None,
                "region": deepcopy(asset.region),
                "caption": asset.caption,
                "width": asset.width,
                "height": asset.height,
                "order_index": asset.order_index,
                "storage_path": asset.storage_path,
                "file_metadata": deepcopy(asset.file_metadata),
            }
            for asset in question.assets
        ],
    }


def _business_context(context: dict[str, Any]) -> dict[str, Any]:
    # Storage migration/path changes do not change the actual question input.
    return {
        key: [
            {
                name: value
                for name, value in asset.items()
                if name not in {"storage_path", "file_metadata"}
            }
            | {"sha256": (asset["file_metadata"] or {}).get("sha256")}
            for asset in values
        ]
        if key == "assets"
        else values
        for key, values in context.items()
    }


class QuestionAdaptationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def prepare(
        self,
        *,
        course_id: UUID,
        actor_id: UUID,
        source_question_id: UUID,
        adaptation_type: AdaptationType,
    ) -> AdaptationSnapshot:
        course = self.session.get(Course, course_id)
        if course is None or course.created_by != actor_id:
            raise QuestionAdaptationError(
                "QUESTION_CANDIDATE_PERMISSION_DENIED", "无权在该课程改编题目。"
            )
        parent = self.session.get(Question, source_question_id)
        if parent is None or parent.course_id != course_id:
            raise QuestionAdaptationError(
                "QUESTION_ADAPTATION_PARENT_INVALID", "父题不存在或不属于当前课程。"
            )
        if adaptation_type not in {"rewrite", "translate", "extend"}:
            raise QuestionAdaptationError(
                "QUESTION_ADAPTATION_TYPE_INVALID", "不支持该改编类型。"
            )
        return AdaptationSnapshot(
            parent.id, course_id, adaptation_type, _context(parent)
        )

    def lock_and_validate(
        self, snapshot: AdaptationSnapshot, *, actor_id: UUID
    ) -> Question:
        course = self.session.scalar(
            select(Course)
            .where(Course.id == snapshot.course_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if course is None or course.created_by != actor_id:
            raise QuestionAdaptationError(
                "QUESTION_CANDIDATE_PERMISSION_DENIED", "课程管理权限已变化。"
            )
        parent = self.session.scalar(
            select(Question)
            .where(Question.id == snapshot.source_question_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if parent is None or parent.course_id != snapshot.course_id:
            raise QuestionAdaptationError(
                "QUESTION_ADAPTATION_PARENT_INVALID", "父题不存在或不属于当前课程。"
            )
        self.session.expire(parent, ["assets"])
        if _business_context(_context(parent)) != _business_context(snapshot.context):
            raise QuestionAdaptationError(
                "QUESTION_ADAPTATION_STALE", "父题内容或原图已变化，请刷新后重新改编。"
            )
        return parent

    def check_edge(
        self, *, course_id: UUID, derived_question_id: UUID, source_question_id: UUID
    ) -> None:
        if derived_question_id == source_question_id:
            raise QuestionAdaptationError(
                "QUESTION_ADAPTATION_SELF_REFERENCE", "父题不能是派生题自身。"
            )
        questions = list(
            self.session.scalars(
                select(Question).where(
                    Question.id.in_([derived_question_id, source_question_id])
                )
            )
        )
        if len(questions) != 2 or any(
            question.course_id != course_id for question in questions
        ):
            raise QuestionAdaptationError(
                "QUESTION_ADAPTATION_PARENT_INVALID", "父子题必须存在并属于同一课程。"
            )
        ancestors: dict[UUID, list[UUID]] = {}
        for edge in self.session.scalars(
            select(QuestionSourcePaper)
            .join(Question, Question.id == QuestionSourcePaper.derived_question_id)
            .where(Question.course_id == course_id)
        ):
            ancestors.setdefault(edge.derived_question_id, []).append(
                edge.source_question_id
            )
        remaining, seen = [source_question_id], set()
        while remaining:
            node = remaining.pop()
            if node == derived_question_id:
                raise QuestionAdaptationError(
                    "QUESTION_ADAPTATION_CYCLE", "父题来源关系不能形成循环。"
                )
            if node not in seen:
                seen.add(node)
                remaining.extend(ancestors.get(node, []))

    def attach(
        self,
        snapshot: AdaptationSnapshot,
        question: Question,
        *,
        actor_id: UUID,
        stored_files: list[StoredFile],
    ) -> None:
        self.check_edge(
            course_id=snapshot.course_id,
            derived_question_id=question.id,
            source_question_id=snapshot.source_question_id,
        )
        self.session.add(
            QuestionSourcePaper(
                derived_question_id=question.id,
                source_question_id=snapshot.source_question_id,
                adaptation_type=snapshot.adaptation_type,
            )
        )
        assets = QuestionAssetService(self.session)
        for source in snapshot.context["assets"]:
            payload = AssetLinkRequest(
                file_id=source["file_id"],
                asset_type=source["asset_type"],
                source_page_id=source["source_page_id"],
                region=PixelRegion.model_validate(source["region"])
                if source["region"]
                else None,
                caption=source["caption"],
                student_visible=False,
            )
            _view, file = assets.prepare_question_link(
                question.id, payload, actor_id=actor_id
            )
            stored_files.append(file)


def parent_source_views(session: Session, question_id: UUID) -> list[dict[str, Any]]:
    return [
        {
            "source_question_id": str(edge.source_question_id),
            "adaptation_type": edge.adaptation_type,
            "created_at": edge.created_at.isoformat(),
        }
        for edge in session.scalars(
            select(QuestionSourcePaper)
            .where(QuestionSourcePaper.derived_question_id == question_id)
            .order_by(QuestionSourcePaper.created_at, QuestionSourcePaper.id)
        )
    ]


def paper_source_view(question: Question | None) -> dict[str, Any] | None:
    if question is None:
        return None
    original = question.imported_extracted_question
    if original is None:
        return None
    paper = original.paper_import
    return {
        "extracted_question_id": str(original.id),
        "paper_import_id": str(paper.id),
        "document_id": str(paper.document_id),
        "document_file_id": "d_" + paper.document_id.hex,
        "source_page_ids": list(original.source_page_ids),
        "question_number": original.question_number,
    }
