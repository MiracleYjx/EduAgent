"""TCR: the vision probe must use a real image, not the 1x1 OCR sample.

A 1x1 PNG passed to a real vision provider can return an opaque provider error
while the same provider accepts a normal image. Keep the OCR sample separate.
"""

from __future__ import annotations

import io

from PIL import Image

from backend.app.services.settings.probe_service import (
    _SAMPLE_PNG,
    _VISION_SAMPLE_PNG,
)


def test_vision_probe_sample_is_valid_rgb_png() -> None:
    image = Image.open(io.BytesIO(_VISION_SAMPLE_PNG))
    assert image.format == "PNG"
    assert image.size == (128, 128)
    assert image.mode == "RGB"


def test_vision_probe_sample_is_distinct_from_ocr_sample() -> None:
    assert _VISION_SAMPLE_PNG != _SAMPLE_PNG
    ocr_image = Image.open(io.BytesIO(_SAMPLE_PNG))
    assert ocr_image.format == "PNG"
