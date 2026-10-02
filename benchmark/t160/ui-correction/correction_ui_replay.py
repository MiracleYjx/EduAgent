"""Prepare new source-backed UI replay records from actual r1 automatic snapshots.
This script does no OCR, extraction, image-understanding, or paid provider call.
Execute only after the independent import-performance worker is finished.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from backend.app.core.config import get_settings
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import ExtractedQuestionStatus, PaperImportStatus
from backend.app.models import ExtractedQuestion, PaperImport
from backend.app.schemas.question_assets import AssetLinkRequest
from backend.app.services.paper_import_service import PaperImportService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quality-pointer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--quality-output",
        type=Path,
        help="Explicit relocated actual quality directory; defaults to pointer output.",
    )
    args = parser.parse_args()
    settings = get_settings()
    assert "eduagent_e2_acceptance_" in str(settings.database_url)
    if args.output.exists():
        raise ValueError(
            "Fresh fixture manifest required; cannot overwrite prior run IDs."
        )
    pointer = json.loads(args.quality_pointer.read_text(encoding="utf-8"))
    quality = (
        args.quality_output.resolve()
        if args.quality_output is not None
        else Path(pointer["output"])
    )
    sources = {}
    for workload, case in [
        ("text", "IMP-TEXT"),
        ("figure", "IMP-IMAGE"),
        ("cross", "IMP-CROSS"),
    ]:
        matches = []
        for p in (quality / "runs").glob("*/result.json"):
            result = json.loads(p.read_text(encoding="utf-8"))
            if result.get("case_id") == case and result.get("repeat_index") == 1:
                matches.append((p, result))
        if len(matches) != 1:
            raise ValueError("Exactly one actual first-round result required: " + case)
        p, result = matches[0]
        raw = result["automatic_snapshot"]
        if result["automatic_status"] != "Pending Review" or not raw["questions"]:
            raise ValueError(
                "Source first-round run did not produce a legitimate correction view."
            )
        sources[workload] = {
            "case_id": case,
            "result_ref": str(p),
            "result_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "raw": raw,
        }
    actors = {s["raw"]["uploaded_by"] for s in sources.values()}
    if len(actors) != 1:
        raise ValueError("Same actual actor required for this fixture batch.")
    actor = UUID(next(iter(actors)))
    output = {
        "prepared_at": datetime.now(UTC).isoformat(),
        "actor_id": str(actor),
        "source_kind": "source_backed_replay_for_ui_timing",
        "quality_output": str(quality),
        "cloud_calls": 0,
        "is_new_extraction": False,
        "instances": [],
    }
    for workload, source in sources.items():
        raw = source["raw"]
        repetitions = (
            [("cold", i, False) for i in range(1, 4)]
            + [("warm", 0, True)]
            + [("warm", i, False) for i in range(1, 6)]
        )
        for temperature, repeat, warmup in repetitions:
            with get_session_factory()() as session:
                service = PaperImportService(session, root=settings.storage_root)
                original = service.get_record(UUID(raw["id"]), actor)
                if original.course_id != UUID(raw["course_id"]):
                    raise ValueError("Actual source ownership mismatch.")
                original_path, _ = service.files.download(
                    raw["original_file_id"], actor_id=actor
                )
                uploaded = service.upload(
                    original.course_id,
                    filename=raw["original_filename"],
                    content=original_path.read_bytes(),
                    actor_id=actor,
                )
                paper = session.get(PaperImport, uploaded.id)
                paper.status = PaperImportStatus.PARSING
                paper.page_count = raw["page_count"]
                session.commit()
                page_map = {}
                for old_page in raw["pages"]:
                    path, _ = service.files.download(
                        old_page["file_id"], actor_id=actor
                    )
                    new = service.assets.create_source_page(
                        paper.id,
                        page_number=old_page["page_number"],
                        content=path.read_bytes(),
                        actor_id=actor,
                    )
                    new.ocr_text = old_page["ocr_text"]
                    new.ocr_confidence = (
                        Decimal(old_page["ocr_confidence"])
                        if old_page["ocr_confidence"] is not None
                        else None
                    )
                    session.commit()
                    page_map[old_page["id"]] = new
                paper.status = PaperImportStatus.PENDING_REVIEW
                session.commit()
                question_map = {}
                for old in raw["questions"]:
                    regions = copy.deepcopy(old["source_regions"])
                    if regions is not None:
                        for region in regions:
                            region["source_page_id"] = str(
                                page_map[region["source_page_id"]].id
                            )
                    columns = {
                        k: copy.deepcopy(old[k])
                        for k in [
                            "question_type",
                            "content",
                            "options",
                            "order_preserved",
                            "reference_answer",
                            "scoring_rubric",
                            "score",
                            "extracted_by",
                            "extraction_confidence",
                            "order_index",
                            "question_number",
                            "analysis",
                            "knowledge_points",
                        ]
                    }
                    record = ExtractedQuestion(
                        id=uuid4(),
                        paper_import_id=paper.id,
                        source_page_ids=[
                            str(page_map[i].id) for i in old["source_page_ids"]
                        ],
                        source_regions=regions,
                        status=ExtractedQuestionStatus.PENDING_CORRECTION,
                        question_id=None,
                        correction_notes=None,
                        assets=[] if old["assets"] is not None else None,
                        image_assessment=None,
                        **columns,
                    )
                    session.add(record)
                    session.commit()
                    question_map[old["id"]] = record
                    for asset in old["assets"] or []:
                        new_page = page_map[asset["source_page_id"]]
                        service.assets.create_staged(
                            record.id,
                            AssetLinkRequest(
                                file_id="p_" + new_page.id.hex,
                                source_page_id=new_page.id,
                                asset_type=asset["asset_type"],
                                region=asset["region"],
                                caption=asset["caption"],
                                student_visible=asset["student_visible"],
                            ),
                            actor_id=actor,
                        )
                refreshed = service.get(paper.id, actor_id=actor)
                by_number = {q.question_number: q for q in refreshed.questions}
                if workload == "text":
                    initial = by_number["2"]
                    target = by_number["1"]
                    action = "question"
                    target_page = 1
                elif workload == "figure":
                    initial = by_number["1"]
                    target = by_number["2"]
                    action = "question"
                    target_page = 1
                else:
                    if len(refreshed.questions) != 1:
                        raise ValueError(
                            "Cross-page replay expects the actual one-question r1 result."
                        )
                    initial = target = refreshed.questions[0]
                    action = "page"
                    target_page = 2
                    if sorted(p.page_number for p in refreshed.pages) != [1, 2]:
                        raise ValueError("Actual cross-page originals missing.")
                key = f"{workload}-" + (
                    "warmup" if warmup else f"{temperature}-{repeat}"
                )
                output["instances"].append(
                    {
                        "instance_key": key,
                        "workload": workload,
                        "case_id": source["case_id"],
                        "temperature": temperature,
                        "repeat_index": repeat,
                        "warmup": warmup,
                        "action_kind": action,
                        "paper_import_id": str(paper.id),
                        "initial_question_id": str(initial.id),
                        "target_question_id": str(target.id),
                        "target_page_number": target_page,
                        "source_actual_paper_id": raw["id"],
                        "source_raw_result_ref": source["result_ref"],
                        "source_raw_result_sha256": source["result_sha256"],
                        "source_kind": "source_backed_replay_for_ui_timing",
                        "is_new_extraction": False,
                        "cloud_calls": 0,
                    }
                )
    assert len(output["instances"]) == 27
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(
        json.dumps({"prepared": 27, "cloud_calls": 0, "output": str(args.output)}),
        flush=True,
    )


if __name__ == "__main__":
    main()
