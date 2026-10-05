"""T183 current-final presentation and explicit missing facts, TCR §38."""

from backend.app.ui.results_diagnosis import current_learning_sections
from backend.app.ui.results_learning import (
    learning_message,
    learning_rows,
    recommendation_rows,
)


def test_actual_answer_zero_and_unknown_are_not_recomputed():
    rows = learning_rows(
        {
            "items": [
                {
                    "order": 1,
                    "answer_id": "a",
                    "student_answer": ["D", "A"],
                    "grading_status": "Confirmed",
                    "score": "0.00",
                    "max_score": "3.00",
                    "lost_score": None,
                    "reason": "实际理由",
                    "published_knowledge_points": None,
                }
            ]
        }
    )
    assert rows[0] == [
        "1",
        '["D", "A"]',
        "Confirmed",
        "0.00",
        "3.00",
        "尚无最终失分",
        "实际理由",
        "未知（待核对）",
    ]


def test_recommendation_shortages_and_real_source_links():
    feedback = {
        "submission_id": "s",
        "recommendations": [
            {
                "knowledge_point": "甲",
                "material_not_ready_reason": "没有资料",
                "practice_not_ready_reason": "没有当前审核练习",
                "materials": [],
                "practices": [],
            }
        ],
    }
    rows, records = recommendation_rows(feedback)
    assert rows == [] and records == []
    assert "没有资料" in learning_message(
        feedback
    ) and "没有当前审核练习" in learning_message(feedback)
    feedback["recommendations"][0]["materials"] = [
        {
            "chunk_id": "c",
            "source": {
                "source_file": "actual.txt",
                "chunk_id": "c",
                "kind": "current_chunk",
            },
            "reason": "失分1.00",
        }
    ]
    rows, records = recommendation_rows(feedback)
    assert rows[0][0:3] == ["复习片段", "甲", "actual.txt"]
    assert records[0] == {"kind": "material", "resource_id": "c", "submission_id": "s"}


def test_nonfinal_and_stale_report_never_manufacture_weak_points():
    for state in ["pending_review", "failed", "insufficient_evidence", "unfinished"]:
        feedback = {
            "is_final": False,
            "processing_status": state,
            "processing_error_code": "FILE_MISSING",
            "processing_reason": "真实处理说明",
            "weak_knowledge_points": [{"knowledge_point": "不能展示"}],
            "mastery_by_knowledge_point": [{"knowledge_point": "不能展示"}],
            "diagnosis": {"status": "Not Ready"},
        }
        weak, report, mastery = current_learning_sections(feedback)
        assert "不能展示" not in weak and mastery == [] and "真实处理说明" in weak
        assert "FILE_MISSING" in learning_message(feedback)
    feedback = {
        "is_final": True,
        "insufficient_evidence_answer_ids": ["a"],
        "mastery_by_knowledge_point": [],
        "weak_knowledge_points": [],
        "diagnosis": {"status": "Stale"},
    }
    weak, report, mastery = current_learning_sections(feedback)
    assert "依据不足" in weak and "已过期" in report and mastery == []
