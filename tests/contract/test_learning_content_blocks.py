"""T020: one ordered content truth, conservative uncertainty and private slots."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.schemas.content_blocks import RevisionContent


def mixed_content():
    asset = str(uuid4())
    return {
        "question_type": "single_choice",
        "stem_blocks": [
            {"type": "text", "text": "before"},
            {"type": "image", "asset_id": asset, "alt": "first figure"},
            {"type": "text", "text": "between"},
            {"type": "image", "asset_id": str(uuid4()), "alt": "second figure"},
            {"type": "formula", "expression": {"kind": "sup", "children": [
                {"kind": "identifier", "text": "x"}, {"kind": "number", "text": "2"}]}},
            {"type": "table", "headers": ["t", "s"], "rows": [["1", "2"], ["2", "4"]]},
        ],
        "options": [
            {"id": "stable-b", "blocks": [{"type": "text", "text": "B"}]},
            {"id": "stable-a", "blocks": [{"type": "image", "asset_id": str(uuid4()), "alt": "option A"}]},
        ],
        "explanation_blocks": [{"type": "text", "text": "private explanation"},
                               {"type": "image", "asset_id": str(uuid4()), "alt": "private figure"}],
    }


def test_content_round_trip_preserves_order_slots_and_stable_ids():
    raw = mixed_content()
    content = RevisionContent.model_validate(raw)
    assert RevisionContent.model_validate(content.model_dump()) == content
    assert [b.type for b in content.stem_blocks] == ["text", "image", "text", "image", "formula", "table"]
    assert [o.id for o in content.options] == ["stable-b", "stable-a"]
    assert len(content.asset_ids()) == 4
    assert len(content.asset_ids(include_explanation=False)) == 3
    assert "private explanation" not in content.text_projection()
    assert "private explanation" in content.text_projection(include_explanation=True)
    assert content.text_projection().index("before") < content.text_projection().index("between")


def test_uncertain_original_is_retained_and_never_projected_as_a_guessed_fact():
    raw = mixed_content()
    raw["stem_blocks"].append({"type": "original", "asset_id": str(uuid4()),
        "reason": "公式无法可靠识别", "source": {"document_id": str(uuid4()), "anchor": "word/document.xml#p[3]/oMath[1]"}})
    content = RevisionContent.model_validate(raw)
    assert content.needs_confirmation
    assert "待确认" in content.text_projection()
    assert content.stem_blocks[-1].source.anchor.endswith("oMath[1]")


@pytest.mark.parametrize("change", [
    lambda x: x.update(question_type="true_false"),
    lambda x: x["options"][1].update(id="stable-b"),
    lambda x: x["stem_blocks"][1].update(url="file:///private.png"),
    lambda x: x["stem_blocks"].append({"type": "image", "alt": "missing identity"}),
    lambda x: x["stem_blocks"].append({"type": "original", "asset_id": str(uuid4()), "reason": "unknown"}),
    lambda x: x["stem_blocks"].append({"type": "formula", "expression": {"kind": "script", "text": "alert(1)"}}),
    lambda x: x["stem_blocks"].append({"type": "formula", "expression": {"kind": "sup", "children": []}}),
    lambda x: x["stem_blocks"].append({"type": "table", "headers": ["a", "b"], "rows": [["1"]]}),
])
def test_invalid_or_unsupported_content_does_not_become_a_question(change):
    raw = deepcopy(mixed_content())
    change(raw)
    with pytest.raises(ValidationError):
        RevisionContent.model_validate(raw)


def test_pure_text_and_subjective_use_the_same_blocks_without_extra_answer_fields():
    content = RevisionContent(question_type="short_answer", stem_blocks=[{"type": "text", "text": "Explain"}])
    assert content.text_projection() == "Explain"
    assert content.asset_ids() == set()
    with pytest.raises(ValidationError):
        RevisionContent.model_validate(content.model_dump() | {"answer_image": "x"})
