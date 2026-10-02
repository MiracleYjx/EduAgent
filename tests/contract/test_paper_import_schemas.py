"""T153 input/unknown semantics, not OCR quality. TCR §10."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.schemas.paper_import import (
    CorrectionPayload,
    ExtractedQuestionData,
    PixelRegion,
)


def test_correction_preserves_omission_null_and_explicit_empty():
    assert CorrectionPayload().model_fields_set == set()
    request = CorrectionPayload(analysis=None, assets=[], knowledge_points=None, source_regions=None)
    assert request.model_fields_set == {"analysis", "assets", "knowledge_points", "source_regions"}
    assert request.assets == [] and request.analysis is None
    data = ExtractedQuestionData(extracted_by="TEXT", question_number="01", analysis=None)
    assert data.question_number == "01" and data.extraction_confidence is None
    assert data.source_regions is None and data.assets is None


@pytest.mark.parametrize("bbox", [[True, 0, 10, 20], [0, 0, float("nan"), 20], [10, 0, 10, 20], [-1, 0, 10, 20]])
def test_pixel_region_rejects_invalid_numeric_input(bbox):
    with pytest.raises(ValidationError):
        PixelRegion(bbox=bbox)


@pytest.mark.parametrize("bad", [{"source_page_ids": None}, {"score": "1.001"}, {"analysis": " "}, {"question_number": " "}, {"question_id": str(uuid4())}, {"image_assessment": {}}])
def test_correction_rejects_invalid_or_server_owned_fields(bad):
    with pytest.raises(ValidationError):
        CorrectionPayload.model_validate(bad)


def test_source_ids_duplicate_and_more_than_five_assets_are_rejected():
    identity = uuid4()
    with pytest.raises(ValidationError):
        CorrectionPayload(source_page_ids=[identity, identity])
    asset = {"id": str(uuid4()), "file_id": "a_" + uuid4().hex, "source_page_id": str(identity), "asset_type": "figure"}
    with pytest.raises(ValidationError):
        CorrectionPayload(assets=[asset] * 6)
