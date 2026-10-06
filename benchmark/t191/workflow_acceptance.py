"""Exercise real LangGraph review using an explicit new synthetic student. Keep prior failed grading receipts."""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--tokens", type=Path, required=True)
    p.add_argument("--prior", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--new-synthetic-student", action="store_true")
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    prior = json.loads(a.prior.read_text("utf8"))
    sid = prior["submission_id"]
    eid = prior["exam_id"]
    tokens = json.loads(a.tokens.read_text("utf8"))
    events = []
    facts = {
        "prior_receipt": str(a.prior.resolve()),
        "course_id": prior["course_id"],
        "exam_id": eid,
        "second_exam_id": prior["second_exam_id"],
        "submission_id": sid,
        "events": events,
        "actor": "AI delegated real DEV roles; not independent teacher",
    }
    c = httpx.Client(base_url=a.url, timeout=240, trust_env=False)

    def save():
        (out / "receipt.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf8"
        )

    def call(method, path, label, *, actor="Teacher", **kw):
        r = c.request(
            method, path, headers={"Authorization": "Bearer " + tokens[actor]}, **kw
        )
        body = r.json()
        events.append({"label": label, "status": r.status_code, "response": body})
        save()
        print(label, r.status_code, flush=True)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"{label}: HTTP {r.status_code}")
        return body

    try:
        if a.new_synthetic_student:
            username = "t191_workflow_" + uuid4().hex[:12]
            password = secrets.token_urlsafe(24)
            user = call(
                "POST",
                "/api/admin/users",
                "create_real_synthetic_student",
                actor="Admin",
                json={
                    "username": username,
                    "email": username + "@example.invalid",
                    "password": password,
                    "roles": ["Student"],
                },
            )
            login = c.post(
                "/api/auth/login", json={"identifier": username, "password": password}
            )
            events.append(
                {
                    "label": "actual_password_login",
                    "status": login.status_code,
                    "response": {
                        "access_token": "[REDACTED]",
                        "user": login.json().get("user"),
                    },
                }
            )
            save()
            if login.status_code != 200:
                raise RuntimeError("Synthetic student login failed")
            tokens["Student"] = login.json()["access_token"]
            (a.tokens.parent / "workflow-student-token.json").write_text(
                json.dumps({"user_id": user["id"], "access_token": tokens["Student"]}),
                encoding="utf8",
            )
            facts["synthetic_student"] = user
            facts["previous_failed_submission_id"] = sid
            start = call(
                "POST",
                "/api/submissions",
                "new_student_start",
                actor="Student",
                json={"exam_id": eid},
            )
            sid = start["id"]
            facts["submission_id"] = sid
            facts["actual_student_answers"] = prior["actual_student_answers"]
            call(
                "POST",
                f"/api/submissions/{sid}/submit",
                "new_student_submit_mixed_answers",
                actor="Student",
                json={"answers": prior["actual_student_answers"]},
            )
        run = call(
            "POST",
            f"/api/workflow/submissions/{sid}/runs",
            "actual_LangGraph_run_and_checkpoint",
            json={"regrade": False},
        )
        facts["workflow"] = run
        queue = call(
            "GET", f"/api/reviews/queue?exam_id={eid}", "actual_workflow_review_queue"
        )
        facts["queue"] = queue
        current_items = [
            item for item in queue["items"] if item["submission_id"] == sid
        ]
        facts["unmodified_other_pending_count"] = len(queue["items"]) - len(
            current_items
        )
        if not current_items:
            raise RuntimeError("EXPECTED_REAL_REVIEW_INTERRUPT")
        for item in current_items:
            call(
                "POST",
                "/api/reviews/decisions",
                "AI_delegated_current_workflow_review",
                json={
                    "submission_id": sid,
                    "answer_id": item["answer_id"],
                    "action": "modify",
                    "score": "2.00",
                    "reason": "会话委托AI依据本场三项各2分核对：实际答案仅给出正确关系式，没有求系数过程和第三组验证，故确认2.00分；不是独立教师。",
                    "workflow_id": run["workflow_id"],
                    "expected_review_status": item["review_status"],
                    "expected_review_round_id": item["pending_review_round_id"],
                },
            )
        facts["final_workflow"] = call(
            "GET", f"/api/workflow/runs/{run['workflow_id']}", "actual_resumed_workflow"
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
        c.close()


if __name__ == "__main__":
    main()
