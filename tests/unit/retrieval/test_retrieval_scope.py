"""T162 explicit scope input and legacy query compatibility."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.ai.retrieval.base import (
    RetrievalInputError,
    RetrievalQuery,
    resolve_filters,
)
from backend.app.schemas.retrieval_scope import RetrievalScope, SectionRange


def test_explicit_scope_defaults_and_exact_label_normalization():
    assert RetrievalScope().is_empty
    scope = RetrievalScope(
        knowledge_points=["  Newton II  ", "Newton II", "newton II", "A  B"]
    )
    assert scope.knowledge_points == ("Newton II", "newton II", "A  B")
    assert not scope.is_empty


@pytest.mark.parametrize("labels", [[" "], [12], "point", None])
def test_scope_rejects_invalid_label_shape(labels):
    with pytest.raises((ValidationError, ValueError)):
        RetrievalScope(knowledge_points=labels)


@pytest.mark.parametrize("start,end", [(True, 2), ("1", 2), (0, 2), (2, 1), (1, False)])
def test_section_range_uses_strict_positive_closed_orders(start, end):
    with pytest.raises(ValidationError):
        SectionRange(chapter_id=uuid4(), start_order=start, end_order=end)


def test_query_carries_scope_without_copying_it_into_public_filters():
    chapter = uuid4()
    section = SectionRange(chapter_id=chapter, start_order=2, end_order=4)
    query = RetrievalQuery(
        " query ",
        (1, 0),
        chapter_ids=(chapter,),
        section_range=section,
        knowledge_points=(" label ",),
    )
    assert query.text == "query"
    assert query.chapter_ids == (chapter,)
    assert query.section_range == section
    assert query.knowledge_points == ("label",)
    assert RetrievalQuery("keyword", ()).embedding == ()


def test_filter_adapter_rejects_unknown_scope_fields_instead_of_ignoring_them():
    with pytest.raises(RetrievalInputError):
        resolve_filters({"chapter_ids": [str(uuid4())]})
