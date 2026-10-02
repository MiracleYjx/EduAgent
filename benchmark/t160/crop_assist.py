"""T160 after-run AI crop assistance; real page bytes, no cloud or teacher checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

parser = argparse.ArgumentParser()
mode = parser.add_mutually_exclusive_group(required=True)
mode.add_argument("--plan-only", action="store_true")
mode.add_argument("--execute-confirmed", action="store_true")
for name in ("run-root", "repo-root", "source-root", "annotations", "quality-output"):
    parser.add_argument("--" + name, type=Path)
args = parser.parse_args()
RUN = (args.run_root or Path(__file__).resolve().parent).resolve()
REPO = (args.repo_root or RUN.parents[1]).resolve()
SOURCE = (args.source_root or REPO).resolve()
ANNOTATIONS = (
    args.annotations
    or REPO / "benchmark/corpus/t160-assisted-20261003/annotations.draft.json"
).resolve()
sys.path[:0] = [str(SOURCE), str(RUN)]
os.chdir(REPO)
from sqlalchemy.engine import make_url

from backend.app.core.config import get_settings
from backend.app.core.database import create_database_engine, create_session_factory
from backend.app.schemas.question_assets import AssetLinkRequest
from backend.app.services.paper_import_service import PaperImportService
from backend.app.services.question_asset_service import QuestionAssetService

# New measured AI operation rectangles from actual 120-DPI original page review.
# These are not teacher-truth source_regions and never change the reference file.
RECTS = {
    "figure": [520 / 993, 400 / 1404, 880 / 993, 660 / 1404],
    "table": [300 / 993, 850 / 1404, 680 / 993, 1130 / 1404],
    "diagram": [330 / 993, 340 / 1404, 630 / 993, 635 / 1404],
}


def now():
    return datetime.now(UTC).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def normalized(value):
    return (
        value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if isinstance(value, str)
        else value
    )


def operation_plan(output, entries):
    rows = []
    for result_path in sorted((output / "runs").glob("*/result.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        paper = result.get("corrected_snapshot")
        if paper is None:
            rows.append(
                {
                    "run_id": result["run_id"],
                    "status": "not_executed",
                    "reason": "no_real_corrected_snapshot",
                    "automatic_status": result.get("automatic_status"),
                }
            )
            continue
        pages = {p["page_number"]: p for p in paper["pages"]}
        for label in entries[result["case_id"]]["labels"]["questions"]:
            wanted = label["assets"]
            if not wanted:
                continue
            matches = [
                q
                for q in paper["questions"]
                if normalized(q["question_number"])
                == normalized(label["question_number"])
            ]
            if len(matches) != 1:
                rows.append(
                    {
                        "run_id": result["run_id"],
                        "reference_identity": label["question_identity"],
                        "status": "not_executed",
                        "reason": "missing_or_duplicate_original_number",
                    }
                )
                continue
            for asset in wanted:
                page = pages.get(asset["source_page_number"])
                if page is None:
                    rows.append(
                        {
                            "run_id": result["run_id"],
                            "reference_identity": label["question_identity"],
                            "status": "not_executed",
                            "reason": "missing_real_source_page",
                        }
                    )
                    continue
                ratio = RECTS[asset["kind"]]
                width, height = page["width"], page["height"]
                box = [
                    round(ratio[0] * width),
                    round(ratio[1] * height),
                    round(ratio[2] * width),
                    round(ratio[3] * height),
                ]
                assert 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
                rows.append(
                    {
                        "run_id": result["run_id"],
                        "case_id": result["case_id"],
                        "paper_import_id": paper["id"],
                        "extracted_question_id": matches[0]["id"],
                        "actor_id": paper["uploaded_by"],
                        "reference_identity": label["question_identity"],
                        "asset_type": asset["kind"],
                        "order_index": asset["order_index"],
                        "source_page_id": page["id"],
                        "source_page_number": page["page_number"],
                        "source_file_id": page["file_id"],
                        "actual_page_size": [width, height],
                        "normalized_crop_rect": ratio,
                        "new_ai_crop_bbox": box,
                        "reference_source_regions_state": "unknown_unchanged",
                        "status": "planned",
                        "student_visible": False,
                        "teacher_review_created": False,
                    }
                )
    return rows


def main():
    if args.quality_output:
        output = args.quality_output.resolve()
    else:
        pointer = json.loads(
            (RUN / "quality-output-pointer.json").read_text(encoding="utf-8")
        )
        output = Path(pointer["output"]).resolve()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    raw = ANNOTATIONS.read_bytes()
    reference = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == manifest["annotation_sha256"]
    assert reference["status"] == "user_confirmed_ai_assisted"
    entries = {e["case_id"]: e for e in reference["entries"]}
    actions = operation_plan(output, entries)
    report = {
        "kind": "after_image_assistance",
        "cloud_calls": 0,
        "independent_teacher_reviews": 0,
        "reference_origin": "user_confirmed_ai_assisted",
        "reference_sha256": manifest["annotation_sha256"],
        "crop_basis": "AI new measurement from original 120-DPI pixels; not original source-region truth",
        "planned_at": now(),
        "actions": actions,
    }
    if args.plan_only:
        save(output / "crop-assistance-plan.json", report)
        print(
            json.dumps(
                {
                    "status": "plan_only",
                    "planned_crops": sum(a["status"] == "planned" for a in actions),
                    "not_executed": sum(a["status"] == "not_executed" for a in actions),
                    "cloud_calls": 0,
                    "plan": str(output / "crop-assistance-plan.json"),
                }
            ),
            flush=True,
        )
        return
    destination = output / "image-assistance"
    destination.mkdir()
    baseline = get_settings()
    isolation = json.loads((RUN / "isolation.json").read_text(encoding="utf-8"))
    assert isolation["database"].startswith("eduagent_e2_acceptance_")
    url = make_url(str(baseline.database_url)).set(database=isolation["database"])
    settings = baseline.model_copy(
        update={
            "database_url": type(baseline.database_url)(
                url.render_as_string(hide_password=False)
            ),
            "storage_root": RUN / "business-files",
        }
    )
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        for i, action in enumerate(actions, 1):
            if action["status"] != "planned":
                continue
            action["started_at"] = now()
            try:
                with factory() as session:
                    service = QuestionAssetService(session, root=settings.storage_root)
                    asset = service.create_staged(
                        UUID(action["extracted_question_id"]),
                        AssetLinkRequest(
                            file_id=action["source_file_id"],
                            source_page_id=UUID(action["source_page_id"]),
                            asset_type=action["asset_type"],
                            region={"bbox": action["new_ai_crop_bbox"]},
                            student_visible=False,
                        ),
                        actor_id=UUID(action["actor_id"]),
                    )
                    path, _ = service.files.download(
                        asset.file_id, actor_id=UUID(action["actor_id"])
                    )
                    from PIL import Image

                    with Image.open(path) as image:
                        width, height = image.size
                    copied = destination / f"crop-{i:03d}-{action['asset_type']}.png"
                    copied.write_bytes(path.read_bytes())
                    action.update(
                        status="persisted",
                        actual_asset=asset.model_dump(mode="json"),
                        actual_crop_size=[width, height],
                        crop_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        crop_evidence_ref=str(copied.relative_to(output)),
                    )
            except Exception as exc:  # noqa: BLE001 - Keep failed cases.
                action.update(
                    status="failed",
                    error={
                        "type": type(exc).__name__,
                        "code": getattr(exc, "code", None),
                        "message": (
                            str(exc)
                            if type(exc).__module__.startswith("backend.")
                            else None
                        ),
                    },
                )
            action["completed_at"] = now()
            save(destination / "actions.json", report)
        snapshots = []
        for result_path in sorted((output / "runs").glob("*/result.json")):
            original = json.loads(result_path.read_text(encoding="utf-8"))
            if not original.get("corrected_snapshot"):
                continue
            stored = original["corrected_snapshot"]
            with factory() as session:
                paper = PaperImportService(session, root=settings.storage_root).get(
                    UUID(stored["id"]), actor_id=UUID(stored["uploaded_by"])
                )
            snapshot = paper.model_dump(mode="json")
            page_numbers = {str(page.id): page.page_number for page in paper.pages}
            association_rows = []
            for label in entries[original["case_id"]]["labels"]["questions"]:
                matched = [
                    q
                    for q in paper.questions
                    if normalized(q.question_number)
                    == normalized(label["question_number"])
                    and [page_numbers[str(identity)] for identity in q.source_page_ids]
                    == label["source_page_numbers"]
                ]
                question = matched[0] if len(matched) == 1 else None
                expected = [
                    {
                        "asset_type": a["kind"],
                        "source_page_number": a["source_page_number"],
                        "order_index": a["order_index"],
                    }
                    for a in label["assets"]
                ]
                actual = (
                    None
                    if question is None or question.assets is None
                    else [
                        {
                            "asset_type": a.asset_type,
                            "source_page_number": page_numbers.get(
                                str(a.source_page_id)
                            ),
                            "order_index": i,
                        }
                        for i, a in enumerate(question.assets, 1)
                    ]
                )
                association_rows.append(
                    {
                        "reference_identity": label["question_identity"],
                        "extracted_question_id": str(question.id) if question else None,
                        "expected": expected,
                        "actual": actual,
                        "evaluable": True,
                        "correct": bool(question is not None and actual == expected),
                        "reason": "ordered type/page association only; independent teacher/pixel correctness not claimed",
                    }
                )
            save(
                destination / f"{original['run_id']}.json",
                {
                    "stage": "after_image_assistance",
                    "snapshot": snapshot,
                    "reference_sha256": manifest["annotation_sha256"],
                    "independent_teacher_review": False,
                    "cloud_calls": 0,
                    "image_association_comparison": association_rows,
                    "image_association_numerator": sum(
                        row["correct"] for row in association_rows
                    ),
                    "image_association_denominator": len(association_rows),
                },
            )
            snapshots.append(
                {
                    "run_id": original["run_id"],
                    "case_id": original["case_id"],
                    "paper_import_id": stored["id"],
                    "snapshot_ref": str(
                        (destination / f"{original['run_id']}.json").relative_to(output)
                    ),
                }
            )
        report.update(
            completed_at=now(),
            snapshots=snapshots,
            persisted_crops=sum(a["status"] == "persisted" for a in actions),
            failed_crops=sum(a["status"] == "failed" for a in actions),
        )
        save(destination / "actions.json", report)
        print(
            json.dumps(
                {
                    "status": "completed_local_assistance",
                    "persisted_crops": report["persisted_crops"],
                    "failed_crops": report["failed_crops"],
                    "cloud_calls": 0,
                    "actions": str(destination / "actions.json"),
                }
            ),
            flush=True,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
