import pytest
from pydantic import ValidationError

from backend.app.schemas.content_blocks import RevisionContent
from backend.app.schemas.learning_charts import LearningChart, review_chart


def chart_data():
    return {"kind": "line", "title": "匀速运动", "x_axis": {"label": "时间", "unit": "s"},
            "y_axis": {"label": "位移", "unit": "m"}, "points": [{"x": 0, "y": 0}, {"x": 1, "y": 2}],
            "basis": "题设给定：t=0时s=0，t=1时s=2"}


def test_finite_chart_requires_explicit_data_axes_units_and_basis():
    chart = LearningChart.model_validate(chart_data())
    assert review_chart(chart, answer_points=chart.points, claimed_consistent=True) == "consistent"
    assert review_chart(chart, answer_points=None, claimed_consistent=True) == "needs_confirmation"
    wrong = chart.model_copy(update={"points": [chart.points[0]]})
    assert review_chart(chart, answer_points=wrong.points, claimed_consistent=True) == "conflicting"
    assert review_chart(chart, answer_points=chart.points, claimed_consistent=False) == "conflicting"
    assert review_chart(chart, answer_points=chart.points, claimed_consistent=None) == "needs_confirmation"


def test_public_chart_has_no_answer_basis_and_does_not_self_authorize_grading():
    content = RevisionContent.model_validate({"question_type": "short_answer", "stem_blocks": [
        {"type": "chart", "chart": chart_data()}]})
    assert content.needs_confirmation
    assert "时间(s)" in content.text_projection() and "位移(m)" in content.text_projection()
    public_chart = content.model_dump()["stem_blocks"][0]["chart"]
    assert "answer_points" not in public_chart and "claimed_consistent" not in public_chart


@pytest.mark.parametrize("change", [
    lambda x: x.update(kind="script"), lambda x: x.update(script="alert(1)"),
    lambda x: x.update(basis=" "), lambda x: x["x_axis"].update(unit=""),
    lambda x: x["points"][0].update(y=float("nan")),
    lambda x: x["points"][0].update(x=True),
    lambda x: x["points"][1].update(x=0), lambda x: x.update(points=[]),
])
def test_chart_rejects_code_missing_basis_nonfinite_or_ambiguous_data(change):
    raw = chart_data()
    change(raw)
    with pytest.raises(ValidationError):
        LearningChart.model_validate(raw)
