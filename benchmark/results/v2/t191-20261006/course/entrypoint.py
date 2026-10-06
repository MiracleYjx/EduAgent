"""Real HTTP course flow on a prepared frozen server; learning-project AI actor."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--tokens", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    tokens = json.loads(args.tokens.read_text("utf-8"))
    client = httpx.Client(base_url=args.url, timeout=240, trust_env=False)
    events = []
    facts = {
        "annotation": "AI-assisted + developer review; not independent teacher",
        "actor": "AI session-delegated, synthetic DEV teacher/student roles",
        "events": events,
    }

    def save():
        (out / "receipt.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def call(
        method, path, label, *, actor="Teacher", allowed=(200, 201, 202, 204), **kwargs
    ):
        started = time.perf_counter()
        response = client.request(
            method, path, headers={"Authorization": "Bearer " + tokens[actor]}, **kwargs
        )
        try:
            body = response.json()
        except ValueError:
            body = {"bytes": len(response.content)}
        event = {
            "label": label,
            "method": method,
            "path": path,
            "status": response.status_code,
            "elapsed_seconds": time.perf_counter() - started,
            "response": body,
        }
        events.append(event)
        save()
        print(label, response.status_code, flush=True)
        if response.status_code not in allowed:
            raise RuntimeError(f"{label}: HTTP {response.status_code}")
        return body

    def validate(qid, chunks, label):
        return call(
            "POST",
            f"/api/questions/{qid}/validations",
            label,
            json={"teaching_chunk_ids": chunks},
        )

    def approve(qid, label):
        return call("POST", f"/api/questions/{qid}/approve", label)

    def image_check(qid, conditions):
        view = call(
            "GET", f"/api/questions/{qid}/image-assessment", "current_image_context"
        )
        run = call(
            "POST",
            f"/api/questions/{qid}/image-understanding",
            "real_vision_model",
            json={},
        )
        facts.setdefault("image_runs", []).append(run)
        view = call(
            "GET", f"/api/questions/{qid}/image-assessment", "current_image_after_model"
        )
        images = view["input_refs"]["images"]
        check = {
            "expected_context_revision": view["context_revision"],
            "expected_run_no": view["run_no"],
            "expected_check_no": view["check_no"],
            "status": "confirmed",
            "confirmed_conditions": [
                {"asset_id": img["asset_id"], "text": conditions} for img in images
            ],
            "image_findings": [
                {
                    "asset_id": img["asset_id"],
                    "finding": "conditions_confirmed",
                    "reason": "会话委托AI直接核对原图条件，非独立教师",
                }
                for img in images
            ],
            "issue_resolutions": [
                {
                    "issue_id": i["issue_id"],
                    "resolution": "resolved",
                    "reason": "AI delegated review of source pixels; not independent teacher",
                }
                for i in view["open_issues"]
            ],
            "explanation": "会话委托AI按真实来源图像核对；学习项目口径，不冒充独立教师。",
        }
        return call(
            "POST",
            f"/api/questions/{qid}/image-manual-checks",
            "ai_delegated_image_check",
            json=check,
        )

    def confirm_basis(eid, assembly):
        for q in assembly["exam_questions"]:
            if q["question_type"] != "SHORT_ANSWER":
                continue
            qid = q["question_id"]
            view = call(
                "GET",
                f"/api/exams/{eid}/questions/{qid}/scoring-basis",
                "scoring_context",
            )
            context = {
                "expected_question_validation_revision": view[
                    "question_validation_revision"
                ],
                "expected_effective_score": view["effective_score"],
                "expected_base_score": view["base_score"],
            }
            prepared = call(
                "POST",
                f"/api/exams/{eid}/questions/{qid}/scoring-basis/prepare",
                "prepare_actual_exam_points",
                json={
                    **context,
                    "expected_basis": view["basis"],
                    "additive": True,
                    "points": [
                        {
                            "key": "relation",
                            "label": "正确关系式",
                            "base_points": "1.00",
                        },
                        {
                            "key": "calculation",
                            "label": "说明代入过程",
                            "base_points": "2.00",
                        },
                    ],
                },
            )
            basis = prepared["basis"]
            call(
                "POST",
                f"/api/exams/{eid}/questions/{qid}/scoring-basis/confirm",
                "confirm_actual_exam_points",
                json={
                    **context,
                    "preparation_id": basis["preparation_id"],
                    "expected_basis": basis,
                    "confirmed_points": [
                        {"key": p["key"], "points": p["default_points"]}
                        for p in basis["points"]
                    ],
                    "reason": "会话委托AI按1+2原标准核对本场6分比例，学习项目口径",
                },
            )

    try:
        course = call(
            "POST",
            "/api/courses",
            "create_real_course",
            json={
                "name": "T191 实际运行的合成数学课程",
                "description": "真实业务/模型闭环；AI辅助开发者审查，不是独立教师质量",
            },
        )
        cid = course["id"]
        facts["course_id"] = cid
        source = root / "benchmark/corpus/v2-draft-20261001/inputs/paper_text.pdf"
        paper = call(
            "POST",
            "/api/paper-imports",
            "import_real_pdf",
            data={"course_id": cid},
            files={"file": (source.name, source.read_bytes(), "application/pdf")},
        )
        pid = paper["id"]
        for _ in range(180):
            paper = call("GET", f"/api/paper-imports/{pid}", "poll_real_import")
            if paper["status"] not in ("Uploaded", "Parsing", "Extracting"):
                break
            time.sleep(1)
        facts["automatic_import"] = paper
        save()
        if paper["status"] != "Pending Review":
            raise RuntimeError("IMPORT_NOT_PENDING_REVIEW")
        labels = json.loads(
            (
                root / "benchmark/corpus/t160-assisted-20261003/annotations.draft.json"
            ).read_text("utf-8")
        )
        labels = next(
            e["labels"]["questions"]
            for e in labels["entries"]
            if e["case_id"] == "IMP-TEXT"
        )
        by_number = {q["question_number"]: q for q in paper["questions"]}
        selected = []
        for label in labels:
            q = by_number.get(label["question_number"])
            if not q:
                raise RuntimeError("SOURCE_QUESTION_IDENTITY_MISSING")
            fields = {
                k: label[k]
                for k in (
                    "question_type",
                    "content",
                    "options",
                    "reference_answer",
                    "analysis",
                    "score",
                    "scoring_rubric",
                    "knowledge_points",
                    "question_number",
                )
            }
            fields.update(
                source_page_ids=q["source_page_ids"],
                source_regions=None,
                correction_notes="授权AI对照冻结原页标注校正，未知边界保持null；非独立教师",
                assets=[],
            )
            call(
                "PATCH",
                f"/api/paper-imports/{pid}/questions/{q['id']}",
                "correct_source_fields",
                json=fields,
            )
            if label["question_number"] == "3":
                page = paper["pages"][0]
                w, h = page["width"], page["height"]
                bbox = [
                    round(w * 300 / 993),
                    round(h * 850 / 1404),
                    round(w * 680 / 993),
                    round(h * 1130 / 1404),
                ]
                call(
                    "POST",
                    f"/api/extracted-questions/{q['id']}/assets",
                    "real_source_table_crop",
                    json={
                        "file_id": page["file_id"],
                        "asset_type": "table",
                        "source_page_id": page["id"],
                        "region": {"bbox": bbox},
                        "student_visible": True,
                    },
                )
            selected.append(q["id"])
        commit = call(
            "POST",
            f"/api/paper-imports/{pid}/commit",
            "idempotent_commit",
            json={"question_ids": selected},
        )
        duplicate = call(
            "POST",
            f"/api/paper-imports/{pid}/commit",
            "repeat_commit",
            json={"question_ids": selected},
        )
        if commit != duplicate:
            raise RuntimeError("COMMIT_NOT_IDEMPOTENT")
        ids = [q["question_id"] for q in commit["questions"]]
        facts["imported_questions"] = ids
        save()
        call(
            "POST",
            f"/api/questions/{ids[1]}/approve",
            "missing_answer_blocked",
            allowed=(409, 422),
        )
        kb = call(
            "POST",
            "/api/knowledge-bases",
            "create_real_knowledge_base",
            json={"course_id": cid, "name": "T191 数学原资料"},
        )
        data = (
            root / "benchmark/corpus/v2-draft-20261001/teaching_basis.md"
        ).read_bytes()
        doc = call(
            "POST",
            f"/api/knowledge-bases/{kb['id']}/documents/upload",
            "real_local_bge_ingestion",
            files={"file": ("teaching_basis.txt", data, "text/plain")},
        )
        facts["ingestion"] = doc
        save()
        if doc["status"] != "Ready":
            raise RuntimeError("TEACHING_INGESTION_NOT_READY")
        chunks = call(
            "GET",
            f"/api/knowledge-bases/documents/{doc['document_id']}/chunks",
            "actual_chunks",
        )
        chunk_ids = [c["id"] for c in chunks]
        facts["chunk_ids"] = chunk_ids
        chapter = call(
            "POST",
            f"/api/knowledge-bases/courses/{cid}/chapters",
            "chapter_directory",
            json={
                "title": "函数与面积",
                "sections": [{"section_order": 1, "title": "一次函数"}],
            },
        )
        for chunk in chunk_ids:
            call(
                "PATCH",
                f"/api/knowledge-bases/chunks/{chunk}/scope",
                "confirmed_chunk_scope",
                json={
                    "chapter_id": chapter["id"],
                    "section_order": 1,
                    "knowledge_points": ["一次函数", "表达与计算"],
                },
            )
        # Supplementary answer is explicitly authored here, never claimed as extracted.
        call(
            "PATCH",
            f"/api/questions/{ids[0]}",
            "supplement_objective_rubric",
            json={
                "scoring_rubric": "选C得2分，其他选项0分。",
                "knowledge_points": ["一次函数"],
            },
        )
        call(
            "POST",
            f"/api/questions/{ids[0]}/submit-review",
            "submit_imported_objective",
        )
        validate(ids[0], chunk_ids, "real_imported_semantic_check")
        approve(ids[0], "approve_imported_objective")
        call(
            "PATCH",
            f"/api/questions/{ids[2]}",
            "explicit_ai_supplement_answer",
            json={
                "reference_answer": "y=2x+1。代入x=1、2、3依次得到3、5、7。",
                "analysis": "由表中三组实际数值验证关系式。",
            },
        )
        image_check(ids[2], "表中x为1、2、3，对应y为3、5、7。")
        call(
            "POST",
            f"/api/questions/{ids[2]}/submit-review",
            "submit_completed_table_question",
        )
        validate(ids[2], chunk_ids, "real_completed_semantic_check")
        approve(ids[2], "approve_table_source")
        request = {
            "course_id": cid,
            "knowledge_points": ["一次函数"],
            "question_type": "SINGLE_CHOICE",
            "count": 1,
            "target_score": "2.00",
            "retrieval_scope": {"chapter_ids": [chapter["id"]]},
        }
        generated = call(
            "POST",
            "/api/question-generation/candidates",
            "real_knowledge_text_generation",
            json=request,
        )
        facts["generation"] = generated
        save()
        new_id = generated["candidates"][0]["candidate_id"]
        original_answer = generated["candidates"][0]["reference_answer"]
        call(
            "PATCH",
            f"/api/questions/{new_id}",
            "change_answer_invalidates_prior_pass",
            json={"reference_answer": "此项为明确错误的失效测试答案"},
        )
        call(
            "POST",
            f"/api/questions/{new_id}/approve",
            "old_validation_cannot_approve",
            allowed=(409, 422),
        )
        call(
            "PATCH",
            f"/api/questions/{new_id}",
            "restore_original_answer_still_requires_new_validation",
            json={"reference_answer": original_answer},
        )
        call(
            "POST",
            f"/api/questions/{new_id}/approve",
            "restored_content_cannot_reuse_old_pass",
            allowed=(409, 422),
        )
        validate(new_id, chunk_ids, "real_new_revision_semantic_check")
        approve(new_id, "approve_real_generated")
        adapted = call(
            "POST",
            "/api/question-generation/adaptations",
            "real_original_question_adaptation",
            json={
                **request,
                "question_type": "SHORT_ANSWER",
                "target_score": "3.00",
                "source_question_id": ids[2],
                "adaptation_type": "rewrite",
            },
        )
        facts["adaptation"] = adapted
        save()
        adapted_id = adapted["candidates"][0]["candidate_id"]
        image_check(adapted_id, "表中x为1、2、3，对应y为3、5、7。")
        validate(adapted_id, chunk_ids, "real_adaptation_semantic_check")
        approve(adapted_id, "approve_real_adaptation")
        exam = call(
            "POST",
            "/api/exams",
            "create_real_exam",
            json={"course_id": cid, "title": "T191 数学混合本机考试"},
        )
        eid = exam["id"]
        facts["exam_id"] = eid
        impossible = call(
            "POST",
            f"/api/exams/{eid}/assemble",
            "unsatisfiable_intent_preserves_draft",
            allowed=(409, 422),
            json={
                "course_id": cid,
                "question_count": 200,
                "type_distribution": [{"question_type": "SINGLE_CHOICE", "count": 200}],
                "total_score": "200.00",
            },
        )
        facts["unsatisfiable_assembly"] = impossible
        preview = call(
            "GET", f"/api/exams/{eid}/assembly-preview", "failed_draft_preview"
        )
        if preview["exam_questions"]:
            raise RuntimeError("FAILED_ASSEMBLY_CHANGED_DRAFT")
        assembly = call(
            "POST",
            f"/api/exams/{eid}/assemble",
            "conditional_assembly",
            json={
                "course_id": cid,
                "question_count": 2,
                "type_distribution": [
                    {"question_type": "SINGLE_CHOICE", "count": 1},
                    {"question_type": "SHORT_ANSWER", "count": 1},
                ],
                "total_score": "10.00",
                "score_overrides": [
                    {"question_id": ids[0], "score": "4.00"},
                    {"question_id": adapted_id, "score": "6.00"},
                ],
            },
        )
        facts["assembly"] = assembly
        save()
        confirm_basis(eid, assembly)
        call("POST", f"/api/exams/{eid}/publish", "publish_local_acceptance_exam")
        second = call(
            "POST",
            "/api/exams",
            "same_questions_second_exam",
            json={
                "course_id": cid,
                "title": "T191 same questions different exam points",
                "question_ids": [ids[0], adapted_id],
            },
        )
        eid2 = second["id"]
        call(
            "PATCH",
            f"/api/exams/{eid2}/questions/{ids[0]}",
            "second_exam_objective_points",
            json={"score": "2.00"},
        )
        second_preview = call(
            "PATCH",
            f"/api/exams/{eid2}/questions/{adapted_id}",
            "second_exam_subjective_points",
            json={"score": "3.00"},
        )
        confirm_basis(eid2, second_preview)
        call("POST", f"/api/exams/{eid2}/publish", "publish_different_exam_points")
        facts["second_exam"] = call(
            "GET", f"/api/exams/{eid2}/assembly-preview", "second_frozen_scores"
        )
        facts["primary_exam_after_second"] = call(
            "GET",
            f"/api/exams/{eid}/assembly-preview",
            "primary_frozen_scores_unchanged",
        )
        if (
            str(facts["second_exam"]["total_score"]) != "5.00"
            or str(facts["primary_exam_after_second"]["total_score"]) != "10.00"
        ):
            raise RuntimeError("EXAM_SPECIFIC_POINTS_MISMATCH")
        student = call(
            "GET",
            f"/api/submissions/exams/{eid}",
            "student_real_exam_and_picture",
            actor="Student",
        )
        facts["student_exam"] = student
        for question in student["questions"]:
            for asset in question["assets"]:
                call(
                    "GET",
                    "/api/files/" + asset["file_id"],
                    "student_authorized_picture_bytes",
                    actor="Student",
                )
            if "reference_answer" in question or "scoring_rubric" in question:
                raise RuntimeError("STUDENT_RESPONSE_LEAKED_ANSWER")
        submission = call(
            "POST",
            "/api/submissions",
            "student_start_exam",
            actor="Student",
            json={"exam_id": eid},
        )
        sid = submission["id"]
        facts["submission_id"] = sid
        save()
        answers = {
            q["question_id"]: (
                "A" if q["question_type"] == "SINGLE_CHOICE" else "y=2x+1。"
            )
            for q in assembly["exam_questions"]
        }
        call(
            "POST",
            f"/api/submissions/{sid}/submit",
            "student_submit_mixed_answers",
            actor="Student",
            json={"answers": answers},
        )
        grading = call(
            "POST",
            f"/api/grading/submissions/{sid}/trigger",
            "real_mixed_grading",
            json={},
        )
        for _ in range(240):
            task = call(
                "GET", f"/api/grading/tasks/{grading['task_id']}", "real_grading_state"
            )
            if task["status"] not in ("Queued", "Running"):
                break
            time.sleep(1)
        facts["grading"] = task
        queue = call(
            "GET", f"/api/reviews/queue?exam_id={eid}", "actual_manual_review_queue"
        )
        facts["review_queue"] = queue
        save()
        if not queue["items"]:
            raise RuntimeError("MANUAL_REVIEW_NOT_EXERCISED")
        for item in queue["items"]:
            call(
                "POST",
                "/api/reviews/decisions",
                "ai_delegated_manual_review",
                json={
                    "submission_id": sid,
                    "answer_id": item["answer_id"],
                    "action": "modify",
                    "score": "2.00",
                    "reason": "会话授权AI复核：只给关系式，未给代入过程；按本场要点2分，不冒充独立教师",
                    "expected_review_status": item["review_status"],
                    "expected_review_round_id": item["pending_review_round_id"],
                },
            )
        facts["teacher_analysis"] = call(
            "GET", f"/api/results/exams/{eid}/summary", "actual_teacher_analysis"
        )
        facts["student_analysis"] = call(
            "GET",
            f"/api/results/me/submissions/{sid}/learning",
            "actual_student_learning",
            actor="Student",
        )
        facts["final_result"] = call(
            "GET",
            f"/api/results/me/submissions/{sid}",
            "actual_final_score",
            actor="Student",
        )
        if (
            not facts["final_result"]["is_final"]
            or str(facts["final_result"]["total_score"]) != "2.00"
        ):
            raise RuntimeError("FINAL_SCORE_NOT_CONFIRMED")
        facts["status"] = "completed"
        save()
    except Exception as exc:
        facts.update(status="failed", failure_type=type(exc).__name__, failure=str(exc))
        save()
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
