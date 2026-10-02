from __future__ import annotations

import asyncio
import subprocess
import sys
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.ai.ingestion.ocr import (
    BaseOCRProvider,
    OCRProviderError,
    OCRProviderInfo,
    OCRRegion,
    OCRResult,
    create_ocr_provider,
)
from tests.unit.settings_helpers import build_test_settings


def test_abstract_contract_and_result_round_trip():
    with pytest.raises(TypeError):
        BaseOCRProvider()
    result = OCRResult(
        text="题干",
        confidence=None,
        regions=[OCRRegion(bbox=(1, 2, 8, 9), text="题干", confidence=Decimal("0.81"))],
    )
    assert OCRResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "confidence", [-0.01, 1.01, float("nan"), float("inf"), "bad", True]
)
def test_bad_confidence_is_rejected(confidence):
    with pytest.raises(ValidationError):
        OCRResult(text="x", confidence=confidence, regions=[])
    with pytest.raises(ValidationError):
        OCRRegion(bbox=(0, 0, 2, 2), text="x", confidence=confidence)


@pytest.mark.parametrize(
    "box",
    [
        (-1, 0, 2, 2),
        (2, 0, 2, 2),
        (3, 0, 2, 2),
        (0, 3, 2, 2),
        (0, 0, float("nan"), 2),
        (0, 0, float("inf"), 2),
        (0, 0, 11, 2),
        ("0", 0, 2, 2),
        (False, 0, 2, 2),
    ],
)
def test_bad_original_page_coordinates_are_rejected(box):
    with pytest.raises(ValidationError):
        OCRResult.model_validate(
            {
                "text": "x",
                "confidence": None,
                "regions": [{"bbox": box, "text": "x", "confidence": None}],
            },
            context={"image_size": (10, 10)},
        )


def test_strict_text_and_readiness():
    with pytest.raises(ValidationError):
        OCRResult(text=42, confidence=None, regions=[])
    with pytest.raises(ValidationError):
        OCRProviderInfo(provider="rapidocr", model=None, ready=False, reason=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"ocr_enabled": True},
        {"ocr_enabled": True, "ocr_provider": "unknown"},
        {"ocr_enabled": True, "ocr_provider": "rapidocr", "ocr_model": "other"},
        {"ocr_enabled": True, "ocr_provider": "rapidocr"},
    ],
)
def test_unavailable_selection_does_not_return_fake_ocr(overrides):
    settings = build_test_settings(
        ocr_enabled=False, ocr_provider=None, ocr_model=None, ocr_model_dir=None
    ).model_copy(update=overrides)
    with pytest.raises(OCRProviderError) as exc:
        create_ocr_provider(settings)
    assert exc.value.code == "OCR_PROVIDER_NOT_READY"
    assert exc.value.message
    assert exc.value.error_code == exc.value.code


def test_explicit_selection_is_lazy_and_configuration_is_log_safe(tmp_path):
    settings = build_test_settings(
        ocr_enabled=True,
        ocr_provider="rapidocr",
        ocr_model="PP-OCRv5-mobile",
        ocr_model_dir=tmp_path / "private-model-directory",
    )
    provider = create_ocr_provider(settings)
    info = provider.describe()
    assert info.provider == "rapidocr" and info.model == "PP-OCRv5-mobile"
    assert not info.ready and info.reason
    assert str(tmp_path) not in info.model_dump_json()
    assert str(tmp_path) not in str(settings.public_dict())
    with pytest.raises(OCRProviderError) as exc:
        asyncio.run(provider.extract_text(tmp_path / "missing.png"))
    assert exc.value.code == "FILE_MISSING"


def test_v1_startup_and_text_parser_do_not_import_optional_sdk():
    code = """
import importlib.abc, sys
class RejectOCR(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'rapidocr', 'onnxruntime'}:
            raise AssertionError('optional OCR dependency imported')
sys.meta_path.insert(0, RejectOCR())
from tests.unit.settings_helpers import build_test_settings
from backend.app.core.app import create_app
from backend.app.ai.ingestion.parsers import parse_document
from backend.app.ai.ingestion.ocr import create_ocr_provider, OCRProviderError
settings = build_test_settings(ocr_enabled=False, ocr_provider=None, ocr_model_dir=None)
assert not settings.ocr_enabled
app = create_app(settings=settings)
assert app is not None
result = parse_document('sample.txt', 'hello OCR-free'.encode())
assert 'hello OCR-free' in result.text
try:
    create_ocr_provider(settings)
except OCRProviderError as exc:
    assert exc.code == 'OCR_PROVIDER_NOT_READY'
else:
    raise AssertionError('disabled OCR returned a provider')
assert 'rapidocr' not in sys.modules and 'onnxruntime' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
