"""Create an explicitly corrected/opened new real adaptation before publication."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--tokens", type=Path, required=True)
    p.add_argument("--prior", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    prior = json.loads(a.prior.read_text("utf8"))
    tokens = json.loads(a.tokens.read_text("utf8"))
    cid = prior["course_id"]
    source = prior["imported_questions"][2]
    objective = prior["generation"]["candidates"][0]["candidate_id"]
    chapter = prior["chapter"]["id"]
    chunks = prior["chunk_ids"]
    events = []
    facts = {
        "course_id": cid,
        "events": events,
        "prior_receipt": str(a.prior.resolve()),
        "actor": "AI session delegated; no independent teacher",
        "synthetic_student_answer": "y=2x+1。",
        "objective_question_id": objective,
    }
    client = httpx.Client(base_url=a.url, timeout=240, trust_env=False)

    def save():
        (out / "receipt.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf8"
        )

    def call(method, path, label, **kw):
        r = client.request(
            method, path, headers={"Authorization": "Bearer " + tokens["Teacher"]}, **kw
        )
        body = r.json()
        events.append({"label": label, "status": r.status_code, "response": body})
        save()
        print(label, r.status_code, flush=True)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"{label}: HTTP {r.status_code}")
        return body

    try:
        generated = call(
            "POST",
            "/api/question-generation/adaptations",
            "new_actual_adaptation_before_publication",
            json={
                "course_id": cid,
                "knowledge_points": ["一次函数"],
                "question_type": "SHORT_ANSWER",
                "count": 1,
                "target_score": "3.00",
                "retrieval_scope": {"chapter_ids": [chapter]},
                "source_question_id": source,
                "adaptation_type": "rewrite",
            },
        )
        facts["adaptation"] = generated
        qid = generated["candidates"][0]["candidate_id"]
        facts["explicit_AI_business_completion"] = call(
            "PATCH",
            f"/api/questions/{qid}",
            "explicit_AI_current_adaptation_completion",
            json={
                "content": "已知y是x的一次函数。根据下表求y关于x的关系式，写出用两组数据求斜率及截距的过程，并代入第三组数据验证。",
                "reference_answer": "由(1,3)、(2,5)得斜率(5-3)/(2-1)=2，截距1，所以y=2x+1。代入第三组x=3得y=7。",
                "scoring_rubric": "正确关系式1.00分；用两组数据求斜率及截距1.00分；代入第三组数据验证1.00分，共3.00分。",
                "analysis": "AI会话委托显式补全业务题；依据实际表格三组点求系数并验证，不称为模型自动提取或独立教师标注。",
            },
        )
        assets = call(
            "GET", f"/api/questions/{qid}/assets", "actual_new_adaptation_assets"
        )
        if (
            len(assets) != 1
            or assets[0]["width"] != 475
            or assets[0]["height"] != 350
            or assets[0]["asset_type"] != "table"
        ):
            raise RuntimeError("ACTUAL_CROP_REQUIRES_REVIEW")
        for asset in assets:
            call(
                "PATCH",
                f"/api/questions/{qid}/assets/{asset['id']}/visibility",
                "open_verified_crop_before_publication",
                json={"student_visible": True},
            )
        call(
            "POST",
            f"/api/questions/{qid}/image-understanding",
            "new_actual_vision_call",
            json={},
        )
        view = call(
            "GET",
            f"/api/questions/{qid}/image-assessment",
            "actual_current_image_context",
        )
        images = view["input_refs"]["images"]
        call(
            "POST",
            f"/api/questions/{qid}/image-manual-checks",
            "AI_delegated_whole_group_check",
            json={
                "expected_context_revision": view["context_revision"],
                "expected_run_no": view["run_no"],
                "expected_check_no": view["check_no"],
                "status": "confirmed",
                "confirmed_conditions": [
                    {
                        "asset_id": i["asset_id"],
                        "text": "实际表格x为1、2、3，对应y为3、5、7。",
                    }
                    for i in images
                ],
                "image_findings": [
                    {
                        "asset_id": i["asset_id"],
                        "finding": "conditions_confirmed",
                        "reason": "AI会话委托复核原页裁图与同源475×350像素，不是独立教师。",
                    }
                    for i in images
                ],
                "issue_resolutions": [
                    {
                        "issue_id": i["issue_id"],
                        "resolution": "resolved",
                        "reason": "已直接核对真实同源表图，不借旧模型报告冒充本轮事实。",
                    }
                    for i in view["open_issues"]
                ],
                "explanation": "AI授权操作，原图与当前补全题干对应；真实来源/身份/时间由服务保存。",
            },
        )
        call(
            "POST",
            f"/api/questions/{qid}/submit-review",
            "submit_actual_completed_adaptation",
        )
        report = call(
            "POST",
            f"/api/questions/{qid}/validations",
            "real_current_semantic_validation",
            json={"teaching_chunk_ids": chunks},
        )
        facts["validation"] = report
        if report["outcome"] != "passed":
            raise RuntimeError("NEW_CURRENT_SEMANTIC_NOT_PASSED")
        call(
            "POST", f"/api/questions/{qid}/approve", "approve_actual_opened_adaptation"
        )
        obj = call("GET", f"/api/questions/{objective}", "actual_generated_objective")
        facts["synthetic_objective_answer"] = obj["options"][0]
        if facts["synthetic_objective_answer"] == obj["reference_answer"]:
            raise RuntimeError("Objective fixture must be wrong")
        exam = call(
            "POST",
            "/api/exams",
            "new_draft_exam_after_visibility_check",
            json={"course_id": cid, "title": "T191 可作答文字生成与带图改编闭环"},
        )
        eid = exam["id"]
        facts["exam_id"] = eid
        facts["assembly"] = call(
            "POST",
            f"/api/exams/{eid}/assemble",
            "actual_new_conditional_assembly",
            json={
                "course_id": cid,
                "question_count": 2,
                "type_distribution": [
                    {"question_type": "SINGLE_CHOICE", "count": 1},
                    {"question_type": "SHORT_ANSWER", "count": 1},
                ],
                "total_score": "10.00",
                "score_overrides": [
                    {"question_id": objective, "score": "4.00"},
                    {"question_id": qid, "score": "6.00"},
                ],
            },
        )
        facts["status"] = "ready_for_exam"
        save()
    except Exception as exc:
        facts.update(status="failed", failure_type=type(exc).__name__, failure=str(exc))
        save()
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
