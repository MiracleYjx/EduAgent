"""Explicit second half of the recorded T191 real course; no invented model output."""

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
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-published", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    prior = json.loads(args.prior.read_text("utf8"))
    tokens = json.loads(args.tokens.read_text("utf8"))
    if (
        prior.get("status") != "ready_for_exam"
        and prior.get("failure") != "confirm_actual_exam_points: HTTP 409"
    ):
        raise ValueError("Expected recorded stale scoring context rejection")
    cid = prior["course_id"]
    eid = prior["exam_id"]
    qid = prior["adaptation"]["candidates"][0]["candidate_id"]
    objective = (
        prior["objective_question_id"]
        if "objective_question_id" in prior
        else prior["imported_questions"][0]
    )
    events = []
    facts = {
        "course_id": cid,
        "exam_id": eid,
        "prior_receipt": str(args.prior.resolve()),
        "events": events,
        "actor": "AI session delegated synthetic DEV roles; not independent teacher",
        "reference": "AI-assisted + developer review learning project",
    }
    client = httpx.Client(base_url=args.url, timeout=240, trust_env=False)

    def save():
        (out / "receipt.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf8"
        )

    def call(
        method, path, label, *, actor="Teacher", allowed=(200, 201, 202, 204), **kwargs
    ):
        started = time.perf_counter()
        r = client.request(
            method, path, headers={"Authorization": "Bearer " + tokens[actor]}, **kwargs
        )
        try:
            body = r.json()
        except ValueError:
            body = {"bytes": len(r.content), "png": r.content.startswith(b"\x89PNG")}
        events.append(
            {
                "label": label,
                "method": method,
                "path": path,
                "status": r.status_code,
                "response": body,
                "elapsed_seconds": time.perf_counter() - started,
            }
        )
        save()
        print(label, r.status_code, flush=True)
        if r.status_code not in allowed:
            raise RuntimeError(f"{label}: HTTP {r.status_code}")
        return body

    def scoring(exam):
        view = call(
            "GET",
            f"/api/exams/{exam}/questions/{qid}/scoring-basis",
            "actual_current_scoring_context",
        )
        if all(part in view["source_rubric"] for part in ("系数2", "常数1", "代入x=3")):
            points = [
                {
                    "key": "coefficient",
                    "label": "x每增加1时y增加2",
                    "base_points": "1.00",
                },
                {"key": "intercept", "label": "x为0时y为1", "base_points": "1.00"},
                {
                    "key": "substitution",
                    "label": "代入x为3得到y为7",
                    "base_points": "1.00",
                },
            ]
        elif all(
            part in view["source_rubric"]
            for part in ("正确关系式1.00", "求斜率及截距1.00", "代入第三组数据验证1.00")
        ):
            points = [
                {"key": "relation", "label": "正确关系式y=2x+1", "base_points": "1.00"},
                {
                    "key": "derivation",
                    "label": "用两组数据求斜率及截距",
                    "base_points": "1.00",
                },
                {
                    "key": "verification",
                    "label": "代入第三组数据验证",
                    "base_points": "1.00",
                },
            ]
        else:
            raise RuntimeError("ACTUAL_RUBRIC_REQUIRES_NEW_OPERATOR_REVIEW")
        if view["basis"] is not None and view["basis"]["confirmation"] is not None:
            return view
        context = {
            "expected_question_validation_revision": view[
                "question_validation_revision"
            ],
            "expected_effective_score": view["effective_score"],
            "expected_base_score": view["base_score"],
        }
        prepared = call(
            "POST",
            f"/api/exams/{exam}/questions/{qid}/scoring-basis/prepare",
            "prepare_actual_three_rubric_items",
            json={
                **context,
                "expected_basis": view["basis"],
                "additive": True,
                "points": points,
            },
        )
        context["expected_base_score"] = prepared["base_score"]
        basis = prepared["basis"]
        return call(
            "POST",
            f"/api/exams/{exam}/questions/{qid}/scoring-basis/confirm",
            "confirm_fresh_prepared_context",
            json={
                **context,
                "preparation_id": basis["preparation_id"],
                "expected_basis": basis,
                "confirmed_points": [
                    {"key": p["key"], "points": p["default_points"]}
                    for p in basis["points"]
                ],
                "reason": "会话委托AI依据当前真实改编Rubric三项各1分核对本场比例；不是独立教师。",
            },
        )

    def objective_scoring(exam):
        view = call(
            "GET",
            f"/api/exams/{exam}/questions/{objective}/scoring-basis",
            "objective_current_context",
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
            f"/api/exams/{exam}/questions/{objective}/scoring-basis/prepare",
            "prepare_objective_actual_points",
            json={
                **context,
                "expected_basis": view["basis"],
                "additive": False,
                "points": [],
            },
        )
        context["expected_base_score"] = prepared["base_score"]
        basis = prepared["basis"]
        return call(
            "POST",
            f"/api/exams/{exam}/questions/{objective}/scoring-basis/confirm",
            "confirm_objective_actual_points",
            json={
                **context,
                "preparation_id": basis["preparation_id"],
                "expected_basis": basis,
                "confirmed_points": [],
                "reason": "AI会话委托核对客观题C为正确选项，按当前本场满分计分，其他选项0；非独立教师。",
            },
        )

    try:
        facts["actual_adapted_question"] = call(
            "GET", f"/api/questions/{qid}", "actual_approved_adaptation"
        )
        if args.resume_published is None:
            facts["primary_scoring"] = scoring(eid)
            facts["primary_objective_scoring"] = objective_scoring(eid)
            call("POST", f"/api/exams/{eid}/publish", "publish_local_exam")
            second = call(
                "POST",
                "/api/exams",
                "same_questions_second_exam",
                json={
                    "course_id": cid,
                    "title": "T191 same questions different actual points",
                    "question_ids": [objective, qid],
                },
            )
            eid2 = second["id"]
            facts["second_exam_id"] = eid2
            call(
                "PATCH",
                f"/api/exams/{eid2}/questions/{objective}",
                "second_objective_points",
                json={"score": "2.00"},
            )
            call(
                "PATCH",
                f"/api/exams/{eid2}/questions/{qid}",
                "second_subjective_points",
                json={"score": "3.00"},
            )
            facts["secondary_scoring"] = scoring(eid2)
            facts["secondary_objective_scoring"] = objective_scoring(eid2)
            call("POST", f"/api/exams/{eid2}/publish", "publish_second_points")
            facts["second_exam"] = call(
                "GET", f"/api/exams/{eid2}/assembly-preview", "second_frozen_scores"
            )
            facts["primary_exam"] = call(
                "GET",
                f"/api/exams/{eid}/assembly-preview",
                "primary_frozen_scores_unchanged",
            )
            if (
                str(facts["second_exam"]["total_score"]) != "5.00"
                or str(facts["primary_exam"]["total_score"]) != "10.00"
            ):
                raise RuntimeError("EXAM_POINTS_NOT_ISOLATED")
        else:
            published = json.loads(args.resume_published.read_text("utf8"))
            if (
                published.get("failure") != "student_real_exam: HTTP 409"
                or published["exam_id"] != eid
            ):
                raise ValueError(
                    "Expected recorded published-exam visibility rejection"
                )
            facts["published_prior_receipt"] = str(args.resume_published.resolve())
            eid2 = facts["second_exam_id"] = published["second_exam_id"]
            facts["primary_exam"] = call(
                "GET",
                f"/api/exams/{eid}/assembly-preview",
                "actual_current_primary_frozen_scores",
            )
            facts["second_exam"] = call(
                "GET",
                f"/api/exams/{eid2}/assembly-preview",
                "actual_current_second_frozen_scores",
            )
            assets = call(
                "GET",
                f"/api/questions/{qid}/assets",
                "actual_adapted_assets_visibility",
            )
            for asset in assets:
                if (
                    asset["asset_type"] != "table"
                    or asset["width"] != 475
                    or asset["height"] != 350
                ):
                    raise ValueError(
                        "Only the actually inspected source table crop may be opened"
                    )
                call(
                    "PATCH",
                    f"/api/questions/{qid}/assets/{asset['id']}/visibility",
                    "AI_explicit_open_verified_crop",
                    json={"student_visible": True},
                )
        student = call(
            "GET", f"/api/submissions/exams/{eid}", "student_real_exam", actor="Student"
        )
        facts["student_exam"] = student
        picture_count = 0
        for q in student["questions"]:
            if "reference_answer" in q or "scoring_rubric" in q:
                raise RuntimeError("STUDENT_ANSWER_LEAK")
            for asset in q["assets"]:
                pic = call(
                    "GET",
                    "/api/files/" + asset["file_id"],
                    "student_real_picture_bytes",
                    actor="Student",
                )
                if not pic.get("png"):
                    raise RuntimeError("STUDENT_IMAGE_NOT_PNG")
                picture_count += 1
        if not picture_count:
            raise RuntimeError("STUDENT_PICTURE_NOT_EXERCISED")
        submission = call(
            "POST",
            "/api/submissions",
            "student_start",
            actor="Student",
            json={"exam_id": eid},
        )
        sid = submission["id"]
        facts["submission_id"] = sid
        answers = {
            objective: prior.get("synthetic_objective_answer", "A"),
            qid: prior.get("synthetic_student_answer", "系数2表示x每增加1，y就增加2。"),
        }
        facts["actual_student_answers"] = answers
        call(
            "POST",
            f"/api/submissions/{sid}/submit",
            "submit_mixed_real_answers",
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
                "GET", f"/api/grading/tasks/{grading['task_id']}", "actual_grading_task"
            )
            if task["status"] not in ("Queued", "Running"):
                break
            time.sleep(1)
        facts["grading"] = task
        if task["status"] != "Completed":
            raise RuntimeError("GRADING_NOT_COMPLETED")
        queue = call("GET", f"/api/reviews/queue?exam_id={eid}", "actual_review_queue")
        facts["queue"] = queue
        if not queue["items"]:
            raise RuntimeError("REAL_MANUAL_REVIEW_NOT_EXERCISED")
        for item in queue["items"]:
            if item["submission_id"] != sid or item["question_id"] != qid:
                raise RuntimeError("Unexpected review item")
            call(
                "POST",
                "/api/reviews/decisions",
                "AI_delegated_actual_manual_review",
                json={
                    "submission_id": sid,
                    "answer_id": item["answer_id"],
                    "action": "modify",
                    "score": "2.00",
                    "reason": "会话委托AI核对：只答对当前评分依据第一项，占本场三项中第一项2分；缺少后两项过程。不冒充独立教师。",
                    "expected_review_status": item["review_status"],
                    "expected_review_round_id": item["pending_review_round_id"],
                },
            )
        facts["teacher_analysis"] = call(
            "GET", f"/api/results/exams/{eid}/summary", "teacher_actual_analysis"
        )
        facts["student_analysis"] = call(
            "GET",
            f"/api/results/me/submissions/{sid}/learning",
            "student_actual_learning",
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
            raise RuntimeError("FINAL_SCORE_MISMATCH")
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
