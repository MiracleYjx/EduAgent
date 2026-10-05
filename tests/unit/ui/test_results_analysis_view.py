"""T182 service values and unknown states, TCR §37."""

from decimal import Decimal

from backend.app.ui.results_analysis import teacher_analysis_values, teacher_answer_rows


def test_service_statistics_are_displayed_without_recounting():
    summary = {
        "eligible_count": 7,
        "participated_count": 6,
        "draft_count": 1,
        "not_participated_count": 1,
        "pending_review_submission_count": 1,
        "failed_submission_count": 1,
        "insufficient_evidence_submission_count": 2,
        "unfinished_submission_count": 1,
        "final_count": 2,
        "final_score_distribution": [
            {"score": Decimal("0.00"), "submission_count": 1},
            {"score": Decimal("4.00"), "submission_count": 1},
        ],
        "question_statistics": [
            {
                "order": 2,
                "exam_question_id": "eq",
                "max_score": Decimal(3),
                "published_knowledge_points": ["甲", "乙"],
                "effective_submission_count": 2,
                "excluded_final_submission_count": 1,
                "awarded_score": Decimal(2),
                "maximum_score": Decimal(6),
                "lost_score": Decimal(4),
                "score_rate": Decimal("0.3333"),
            }
        ],
        "knowledge_point_statistics": [
            {
                "knowledge_point": "甲",
                "exam_question_ids": ["eq"],
                "effective_student_count": 2,
                "effective_answer_count": 2,
                "awarded_score": Decimal(2),
                "maximum_score": Decimal(6),
                "lost_score": Decimal(4),
                "score_rate": Decimal("0.3333"),
            }
        ],
        "attention_students": [
            {
                "student_id": "s",
                "submission_id": "sub",
                "reasons": ["失败"],
                "final_lost_score": None,
                "error_code": "FILE_MISSING",
                "answer_ids": ["a"],
            }
        ],
    }
    overview, distribution, questions, points, attention, records = (
        teacher_analysis_values(summary)
    )
    assert (
        "可参加：7" in overview
        and "已参加：6" in overview
        and "处理失败：1" in overview
    )
    assert distribution == [["0.00", "1"], ["4.00", "1"]]
    assert questions[0][4:10] == ["2", "1", "2", "6", "4", "0.3333"]
    assert points[0][2:] == ["2", "2", "2", "6", "4", "0.3333"]
    assert "不能加总" in overview and "最终答卷" in overview
    assert attention[0][3:5] == ["暂无最终失分", "FILE_MISSING"]
    assert records == summary["attention_students"]


def test_zero_observation_stays_unknown_and_clears_tables():
    values = teacher_analysis_values(None)
    assert "暂无" in values[0] and all(value == [] for value in values[1:])
    values = teacher_analysis_values(
        {
            "final_count": 0,
            "question_statistics": [
                {
                    "order": 1,
                    "published_knowledge_points": None,
                    "max_score": None,
                    "effective_submission_count": 0,
                    "excluded_final_submission_count": 0,
                }
            ],
        }
    )
    assert "暂无最终分数分布" in values[0]
    assert values[2][0][2:4] == ["未知（待核对）", "未知（待核对）"]
    assert values[2][0][4] == "0" and values[2][0][6] == "暂无数据"


def test_actual_answers_keep_zero_and_unknown_without_computing_loss():
    assert teacher_answer_rows(None) == []
    detail = {
        "result": {
            "items": [
                {
                    "order": 1,
                    "answer_id": "a",
                    "grading_status": "Confirmed",
                    "effective_score": Decimal(0),
                    "max_score": Decimal(3),
                    "reason": "解释",
                }
            ]
        },
        "answers": {"a": ["B", "A"]},
    }
    row = teacher_answer_rows(detail)[0]
    assert row == ["1", '["B", "A"]', "Confirmed", "0", "3", "解释", "a"]


def test_wrong_role_cannot_load_extended_teacher_statistics():
    from backend.app.ui import results_view as view

    called = []
    view.configure_results_loaders(
        teacher_summary_loader=lambda *args: called.append(args)
        or {"eligible_count": 99}
    )
    try:
        values = view.refresh_teacher_panel(
            "c", "e", {"access_token": "token", "roles": ["Student"], "user_id": "s"}
        )
        assert called == [] and "暂无考情" in values[16]
    finally:
        view.configure_results_loaders()
