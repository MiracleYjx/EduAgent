"""Execute the frozen T146 assembly cases against real services and PostgreSQL."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sqlalchemy import select

from backend.app.core.database import get_session_factory
from backend.app.models import Question
from backend.app.schemas.exam_assembly import AssemblyRequest, ExamQuestionPatchRequest
from backend.app.services.exam_assembly_service import AssemblyError
from backend.app.services.exam_service import ExamService
from benchmark.t179.fixtures import CORPUS, require_isolation


def facts(view):
    return [
        {
            k: q.model_dump(mode="json")[k]
            for k in [
                "id",
                "question_id",
                "order_index",
                "score",
                "base_score",
                "scoring_basis",
                "published_knowledge_points",
                "assets",
            ]
        }
        for q in view.exam_questions
    ]


def check_request(view, request):
    assert len(view.exam_questions) == request.question_count
    assert [q.order_index for q in view.exam_questions] == list(
        range(1, request.question_count + 1)
    )
    assert Counter(q.question_type.value for q in view.exam_questions) == Counter(
        {r.question_type: r.count for r in request.type_distribution}
    )
    assert (
        sum((q.effective_score for q in view.exam_questions), Decimal(0))
        == request.total_score
    )
    for r in request.knowledge_coverage:
        assert (
            sum(r.knowledge_point in q.knowledge_points for q in view.exam_questions)
            >= r.min_questions
        )
    assert all(c.satisfied for c in view.conditions)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--verify-exam")
    args = p.parse_args()
    require_isolation()
    spec = json.loads(args.fixtures.read_text(encoding="utf-8"))
    actor = spec["teacher_id"]
    cid = spec["course_id"]
    if args.output.exists():
        raise ValueError("Use a fresh output; no overwrite")
    if args.verify_exam:
        with get_session_factory()() as session:
            view = ExamService(session).preview_assembly(
                args.verify_exam, teacher_id=actor
            )
        args.output.write_text(
            json.dumps(
                {
                    "pid": __import__("os").getpid(),
                    "read_at": datetime.now(UTC).isoformat(),
                    "preview": view.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return
    source = json.loads((CORPUS / "assembly_cases.json").read_text(encoding="utf-8"))
    pool = json.loads((CORPUS / "question_pool.json").read_text(encoding="utf-8"))[
        "candidates"
    ]
    qids = [r["question_id"] for r in pool]
    report = {
        "source": "AI assisted + developer review; independent teacher=0",
        "started_at": datetime.now(UTC).isoformat(),
        "planned_cases": [r["case_id"] for r in source],
        "cases": [],
    }
    for case in source:
        row = {"case_id": case["case_id"], "started_at": datetime.now(UTC).isoformat()}
        try:
            with get_session_factory()() as session:
                service = ExamService(session)
                initial = case.get("initial_draft", qids[:2])
                exam = service.create_exam(
                    cid,
                    "T179 " + case["case_id"] + " business",
                    teacher_id=actor,
                    question_ids=initial,
                )
                before = service.preview_assembly(exam.id, teacher_id=actor)
                row["before"] = before.model_dump(mode="json")
                row["exam_id"] = exam.id
                if case["case_id"] == "ASM-LEGACY":
                    assert (
                        before.assembly_constraints is None
                        and before.conditions == []
                        and before.total_score == Decimal("2.00")
                    )
                    after = before
                else:
                    request = AssemblyRequest.model_validate(case["request"])
                    is_unsat = case.get("draft_unsatisfiable_reason") is not None
                    if is_unsat:
                        try:
                            service.assemble_exam(exam.id, request, teacher_id=actor)
                        except AssemblyError as error:
                            assert (
                                error.code == "EXAM_ASSEMBLY_UNSATISFIED"
                                and error.details["intent_saved"] is True
                            )
                            row["expected_rejection"] = error.as_detail()
                        else:
                            raise AssertionError(
                                "Mathematically impossible missing label request succeeded"
                            )
                        after = service.preview_assembly(exam.id, teacher_id=actor)
                        assert facts(before) == facts(after)
                        assert after.assembly_constraints.request == request
                        assert after.assembly_constraints.recorded_by == UUID(actor)
                        assert any(not c.satisfied for c in after.conditions)
                        assert all(
                            "未收录知识点" not in r["knowledge_points"] for r in pool
                        )
                        if case["case_id"] == "ASM-FAIL-RESTART":
                            session.rollback()
                            fresh = args.output.parent / "failure-fresh-process.json"
                            subprocess.run(
                                [
                                    sys.executable,
                                    str(Path(__file__)),
                                    "--fixtures",
                                    str(args.fixtures),
                                    "--output",
                                    str(fresh),
                                    "--verify-exam",
                                    exam.id,
                                ],
                                check=True,
                            )
                            read = json.loads(fresh.read_text(encoding="utf-8"))
                            from backend.app.schemas.exam_assembly import (
                                AssemblyResponse,
                            )

                            reopened = AssemblyResponse.model_validate(read["preview"])
                            assert (
                                facts(reopened) == facts(before)
                                and reopened.assembly_constraints
                                == after.assembly_constraints
                            )
                            row["fresh_process_read"] = str(fresh)
                    else:
                        after = service.assemble_exam(
                            exam.id, request, teacher_id=actor
                        )
                        check_request(after, request)
                        if case["case_id"] == "ASM-EDIT":
                            # Legitimate teacher setup restores the corpus's explicit initial selection.
                            service.remove_questions(
                                exam.id,
                                [str(q.question_id) for q in after.exam_questions],
                                teacher_id=actor,
                            )
                            service.add_questions(exam.id, initial, teacher_id=actor)
                            action_trace = []
                            for action in case["actions"]:
                                patch = (
                                    {
                                        "replacement_question_id": action[
                                            "new_question_id"
                                        ]
                                    }
                                    if action["kind"] == "replace"
                                    else {"order_index": action["to_index"] + 1}
                                )
                                qid = (
                                    action["old_question_id"]
                                    if action["kind"] == "replace"
                                    else action["question_id"]
                                )
                                changed = service.patch_exam_question(
                                    exam.id,
                                    qid,
                                    ExamQuestionPatchRequest.model_validate(patch),
                                    teacher_id=actor,
                                )
                                action_trace.append(
                                    {
                                        "action": action,
                                        "actual_patch": patch,
                                        "preview": changed.model_dump(mode="json"),
                                    }
                                )
                            after = service.preview_assembly(exam.id, teacher_id=actor)
                            assert [
                                str(q.question_id) for q in after.exam_questions
                            ] == case["draft_witness"]
                            check_request(after, request)
                            row["actions"] = action_trace
                        if case["case_id"] == "ASM-OVERRIDE":
                            selected = next(
                                q
                                for q in after.exam_questions
                                if str(q.question_id) == qids[0]
                            )
                            assert selected.effective_score == Decimal("2.00")
                            other = service.create_exam(
                                cid,
                                "T179 same question different exam",
                                teacher_id=actor,
                                question_ids=[qids[0]],
                            )
                            other_view = service.preview_assembly(
                                other.id, teacher_id=actor
                            )
                            assert other_view.exam_questions[
                                0
                            ].effective_score == Decimal("1.00")
                            row["other_exam"] = other_view.model_dump(mode="json")
                    row["request"] = request.model_dump(mode="json")
                row["after"] = after.model_dump(mode="json")
                # Bank amount and inputs never change merely because a draft was assembled.
                actual = session.scalars(
                    select(Question).where(Question.course_id == UUID(cid))
                ).all()
                byid = {str(q.id): q for q in actual}
                assert len(actual) == 300
                assert all(
                    byid[q["question_id"]].score == Decimal(q["score"]) for q in pool
                )
                row["passed"] = True
        except Exception as error:  # noqa: BLE001
            row["passed"] = False
            row["error"] = {
                "type": type(error).__name__,
                "code": getattr(error, "code", None),
                "message": str(error),
            }
        row["completed_at"] = datetime.now(UTC).isoformat()
        report["cases"].append(row)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    report["passed_count"] = sum(r["passed"] for r in report["cases"])
    report["planned_count"] = len(source)
    report["passed"] = report["passed_count"] == len(source)
    report["completed_at"] = datetime.now(UTC).isoformat()
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "passed": report["passed_count"],
                "planned": len(source),
                "result": str(args.output),
            }
        )
    )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
