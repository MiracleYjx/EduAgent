"""T179 original corpus, real service eligibility, explicitly controlled inference.

Requires an operator-owned disposable eduagent_t179_* database and storage root.
Original corpus/annotation files are read only. These are business fixtures, not
independent teacher labels or a model quality measurement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import (
    DocumentStatus,
    QuestionSourceType,
    QuestionStatus,
    QuestionType,
    UserRole,
)
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Question,
    QuestionAsset,
    QuestionSourceChunk,
)
from backend.app.schemas.image_assessment import (
    ConfirmedCondition,
    ImageAssessment,
    ImageFinding,
    ImageManualCheck,
)
from backend.app.services.auth_service import ensure_dev_mode_accounts
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.question_service import QuestionService
from tests.support.question_validation_fixtures import persist_current_semantic_pass

CORPUS = ROOT / "benchmark/corpus/v2-draft-20261001"
ANNOTATIONS = (
    ROOT / "benchmark/corpus/t146-ai-authorized-20261003/assembly_annotations.json"
)


def require_isolation():
    from sqlalchemy.engine import make_url

    settings = get_settings()
    if not make_url(str(settings.database_url)).database.startswith("eduagent_t179_"):
        raise ValueError("Refuse business fixture creation outside owned T179 database")
    return settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refuse to overwrite a fixture receipt")
    settings = require_isolation()
    pool = json.loads((CORPUS / "question_pool.json").read_text(encoding="utf-8"))
    annotations = json.loads(ANNOTATIONS.read_text(encoding="utf-8"))
    assert len(pool["candidates"]) == 300
    assert len(annotations["candidate_annotations"]) == 300
    for path, digest in annotations["source_files"].items():
        assert hashlib.sha256((CORPUS / path).read_bytes()).hexdigest() == digest
    receipt = {
        "created_at": datetime.now(UTC).isoformat(),
        "source": "T146 AI assisted + developer review; independent teachers=0",
        "controlled_inference": "existing StubSemanticProvider; no cloud calls; not quality evidence",
        "source_hashes": annotations["source_files"],
        "approved_questions": [],
        "actual_manual_image_checks": [],
    }
    with get_session_factory()() as session:
        accounts = ensure_dev_mode_accounts(session, dev_mode=True)
        teacher = accounts[UserRole.TEACHER].id
        student = accounts[UserRole.STUDENT].id
        cid = UUID(pool["course_id"])
        assert session.get(Course, cid) is None
        course = Course(
            id=cid,
            name="T179 corpus assembly acceptance (synthetic)",
            created_by=teacher,
        )
        session.add(course)
        session.flush()
        kb = KnowledgeBase(course_id=cid, name="Original T146 synthetic teaching basis")
        session.add(kb)
        session.flush()
        raw = (CORPUS / "teaching_basis.md").read_bytes()
        rel = "documents/t179_teaching_basis.md"
        target = settings.storage_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        doc = Document(
            course_id=cid,
            knowledge_base_id=kb.id,
            uploaded_by=teacher,
            original_filename="teaching_basis.md",
            file_format="md",
            status=DocumentStatus.READY,
            storage_path=rel,
            file_metadata={
                "media_type": "text/markdown",
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "migration": {"status": "not_required", "latest_attempt": None},
            },
        )
        session.add(doc)
        session.flush()
        chunk = DocumentChunk(
            document_id=doc.id,
            course_id=cid,
            knowledge_base_id=kb.id,
            chunk_index=0,
            content=raw.decode("utf-8"),
            chunk_metadata={"fixture_origin": "T146 synthetic teaching_basis.md"},
        )
        session.add(chunk)
        session.commit()
        for row in pool["candidates"]:
            q = Question(
                id=UUID(row["question_id"]),
                course_id=cid,
                type=QuestionType(row["question_type"]),
                content=row["content"],
                options=row["options"],
                reference_answer=row["reference_answer"],
                scoring_rubric=row["scoring_rubric"],
                score=Decimal(row["score"]),
                knowledge_points=row["knowledge_points"],
                source_type=QuestionSourceType.MANUAL,
                status=QuestionStatus.PENDING_REVIEW,
                created_by=teacher,
            )
            session.add(q)
            session.flush()
            session.add(
                QuestionSourceChunk(
                    question_id=q.id,
                    chunk_id=chunk.id,
                    live_chunk_id=chunk.id,
                    document_id=doc.id,
                    course_id=cid,
                    source_order=0,
                    content_snapshot=chunk.content,
                    source_file="teaching_basis.md",
                    chunk_index=0,
                    retrieval_rank=1,
                    score_kind="controlled_fixture",
                    score_value=Decimal(1),
                )
            )
            for index, image in enumerate(row["image_refs"], start=1):
                from PIL import Image

                data = (CORPUS / image).read_bytes()
                with Image.open(CORPUS / image) as pixels:
                    width, height = pixels.size
                rel = f"assets/t179_{q.id}_{index}.png"
                target = settings.storage_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                asset = QuestionAsset(
                    question=q,
                    asset_type="diagram",
                    width=width,
                    height=height,
                    caption="Original T146 synthetic diagram",
                    order_index=index,
                    student_visible=True,
                    _file_path=rel,
                    _file_metadata={
                        "media_type": "image/png",
                        "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "migration": {"status": "not_required", "latest_attempt": None},
                    },
                )
                session.add(asset)
                session.flush()
            session.commit()
            if q.assets:
                service = ContentValidationService(session, root=settings.storage_root)
                refs, _ = service._read_images(service._image_refs(q), teacher)
                check = ImageManualCheck(
                    id=uuid4(),
                    check_no=1,
                    context_revision=0,
                    run_no=0,
                    run_id=None,
                    input_refs=refs,
                    status="confirmed",
                    confirmed_conditions=[
                        ConfirmedCondition(
                            condition_id=uuid4(),
                            asset_id=q.assets[0].id,
                            text="输入x，经y=2*x+1，输出y；与原夹具公式一致。",
                            evidence_region=None,
                            source_condition_id=None,
                        )
                    ],
                    image_findings=[
                        ImageFinding(
                            asset_id=q.assets[0].id,
                            finding="conditions_confirmed",
                            reason="Explicit AI-authored synthetic fixture condition",
                        )
                    ],
                    issues=[],
                    issue_resolutions=[],
                    teacher_id=teacher,
                    checked_at=datetime.now(UTC),
                    explanation="AI authored business setup under developer delegation; not independent teacher review",
                )
                q.image_assessment = ImageAssessment(manual_checks=[check]).model_dump(
                    mode="json"
                )
                session.commit()
                receipt["actual_manual_image_checks"].append(
                    check.model_dump(mode="json")
                )
            report = persist_current_semantic_pass(
                session, q.id, teacher, root=settings.storage_root
            )
            QuestionService(session).update_question_status(
                q.id, QuestionStatus.APPROVED, teacher_id=teacher
            )
            receipt["approved_questions"].append(
                {
                    "question_id": str(q.id),
                    "validation_revision": q.validation_revision,
                    "validation_result_id": str(report.id),
                    "status": q.status.value,
                }
            )
        actual = list(
            session.scalars(select(Question).where(Question.course_id == cid))
        )
        assert len(actual) == 300 and all(
            q.status == QuestionStatus.APPROVED for q in actual
        )
        by_id = {str(q.id): q for q in actual}
        for row in pool["candidates"]:
            q = by_id[row["question_id"]]
            assert (
                q.content,
                q.options,
                q.reference_answer,
                q.scoring_rubric,
                q.score,
                q.knowledge_points,
            ) == (
                row["content"],
                row["options"],
                row["reference_answer"],
                row["scoring_rubric"],
                Decimal(row["score"]),
                row["knowledge_points"],
            )
        receipt.update(
            teacher_id=str(teacher),
            student_id=str(student),
            course_id=str(cid),
            candidate_count=300,
            actual_database_loaded=True,
            completed_at=datetime.now(UTC).isoformat(),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "candidate_count": 300,
                "fixture_receipt": str(args.output),
                "cloud_calls": 0,
                "independent_teachers": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
