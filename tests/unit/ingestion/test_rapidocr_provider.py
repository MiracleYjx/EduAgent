from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from backend.app.ai.ingestion.ocr import OCRProviderError
from backend.app.ai.ingestion.ocr import rapidocr as adapter


@dataclass
class NativeResult:
    boxes: Any = None
    txts: Any = None
    scores: Any = None


@pytest.fixture
def page(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (100, 200), "white").save(path)
    return path


def native_result(**changes):
    values = {
        "boxes": [
            [[2, 3], [50, 3], [50, 30], [2, 30]],
            [[5, 40], [90, 40], [90, 90], [5, 90]],
        ],
        "txts": ("第二列", "第一列"),
        "scores": (0.0, 0.93),
    }
    values.update(changes)
    return NativeResult(**values)


def provider_with_output(monkeypatch, tmp_path, output=None, error=None):
    calls = []

    def engine(image, **kwargs):
        calls.append((image.size, kwargs))
        if error is not None:
            raise error
        return output

    monkeypatch.setattr(
        adapter, "_load_engine", lambda model_dir: (engine, NativeResult)
    )
    return adapter.RapidOCRProvider(tmp_path), calls


def test_native_order_pixels_and_unknown_aggregate(monkeypatch, tmp_path, page):
    provider, calls = provider_with_output(monkeypatch, tmp_path, native_result())
    assert not provider.describe().ready
    result = asyncio.run(provider.extract_text(page))
    assert result.text == "第二列\n第一列"
    assert result.confidence is None
    assert result.regions[0].bbox == (2, 3, 50, 30)
    assert [r.confidence for r in result.regions] == [Decimal("0.0"), Decimal("0.93")]
    assert calls == [((100, 200), {"text_score": 0.0})]
    assert provider.describe().ready and provider.describe().reason is None


@pytest.mark.parametrize("raw", [NativeResult(), NativeResult([], (), ())])
def test_real_empty_shape_is_valid(monkeypatch, tmp_path, page, raw):
    provider, _ = provider_with_output(monkeypatch, tmp_path, raw)
    result = asyncio.run(provider.extract_text(page))
    assert result.text == "" and result.regions == [] and result.confidence is None


def test_unknown_regional_confidence_remains_null(monkeypatch, tmp_path, page):
    provider, _ = provider_with_output(
        monkeypatch, tmp_path, native_result(scores=None)
    )
    result = asyncio.run(provider.extract_text(page))
    assert all(r.confidence is None for r in result.regions)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        SimpleNamespace(boxes=None, txts=None, scores=None),
        native_result(txts=None),
        native_result(boxes=None),
        native_result(txts=("x",)),
        native_result(scores=(0.9,)),
        native_result(txts=(9, "x")),
        native_result(scores=(float("nan"), 0.8)),
        native_result(scores=(1.1, 0.8)),
        native_result(scores=("0.9", 0.8)),
        native_result(boxes=[[[0, 0], [101, 0], [101, 3], [0, 3]]] * 2),
        native_result(boxes=[[[0, 0], [2, 0], [float("inf"), 3], [0, 3]]] * 2),
        native_result(boxes=[[[0, 0], [2, 0], [2, 3]]] * 2),
        NativeResult(None, (), None),
        NativeResult([], {}, ()),
        NativeResult({}, (), ()),
        NativeResult([], (), ""),
    ],
)
def test_bad_or_partial_sdk_output_is_not_empty_success(
    monkeypatch, tmp_path, page, raw
):
    provider, _ = provider_with_output(monkeypatch, tmp_path, raw)
    with pytest.raises(OCRProviderError) as exc:
        asyncio.run(provider.extract_text(page))
    assert exc.value.code == "OCR_OUTPUT_INVALID"
    assert exc.value.__cause__ is not None


def test_true_sdk_failure_preserves_cause_without_path_disclosure(
    monkeypatch, tmp_path, page
):
    error = TimeoutError(f"internal path {tmp_path}")
    provider, calls = provider_with_output(monkeypatch, tmp_path, error=error)
    with pytest.raises(OCRProviderError) as exc:
        asyncio.run(provider.extract_text(page))
    assert exc.value.code == "OCR_CALL_FAILED" and exc.value.__cause__ is error
    assert str(tmp_path) not in str(exc.value)
    assert len(calls) == 1


def test_invalid_image_never_calls_sdk(monkeypatch, tmp_path):
    provider, calls = provider_with_output(monkeypatch, tmp_path, NativeResult())
    path = tmp_path / "bad.png"
    path.write_bytes(b"not an image")
    with pytest.raises(OCRProviderError) as exc:
        asyncio.run(provider.extract_text(path))
    assert exc.value.code == "OCR_INPUT_INVALID" and exc.value.__cause__ is not None
    assert calls == []


@pytest.mark.parametrize(
    "failure",
    [
        ModuleNotFoundError("rapidocr missing"),
        OSError("ONNX DLL unavailable"),
        FileNotFoundError("missing model"),
        ValueError("model checksum mismatch"),
    ],
)
def test_loading_failure_is_not_ready_with_real_cause(
    monkeypatch, tmp_path, page, failure
):
    def load(model_dir):
        raise failure

    monkeypatch.setattr(adapter, "_load_engine", load)
    provider = adapter.RapidOCRProvider(tmp_path)
    with pytest.raises(OCRProviderError) as exc:
        asyncio.run(provider.extract_text(page))
    assert exc.value.code == "OCR_PROVIDER_NOT_READY" and exc.value.__cause__ is failure
    assert not provider.describe().ready and provider.describe().reason


def test_model_identity_checks_missing_and_wrong_bytes(tmp_path):
    with pytest.raises(FileNotFoundError):
        adapter._model_paths(tmp_path)
    for name in adapter.MODEL_SHA256:
        (tmp_path / name).write_bytes(b"not an ONNX model")
    with pytest.raises(ValueError, match="checksum"):
        adapter._model_paths(tmp_path)


def test_explicit_cpu_parameters_do_not_allow_sdk_download(monkeypatch, tmp_path):
    paths = {name: tmp_path / name for name in adapter.MODEL_SHA256}
    monkeypatch.setattr(adapter, "_model_paths", lambda model_dir: paths)
    parameters = []
    sdk = SimpleNamespace(
        RapidOCR=lambda *, params: parameters.append(params) or object(),
        EngineType=SimpleNamespace(ONNXRUNTIME="onnxruntime"),
        ModelType=SimpleNamespace(MOBILE="mobile"),
        OCRVersion=SimpleNamespace(PPOCRV5="PP-OCRv5", PPOCRV4="PP-OCRv4"),
    )

    def load(name):
        return (
            sdk if name == "rapidocr" else SimpleNamespace(RapidOCROutput=NativeResult)
        )

    monkeypatch.setattr(adapter.importlib, "import_module", load)
    engine, result_type = adapter._load_engine(tmp_path)
    assert engine is not None and result_type is NativeResult
    params = parameters[0]
    for stage in ("Det", "Rec", "Cls"):
        assert Path(params[f"{stage}.model_path"]) in paths.values()
        assert params[f"{stage}.engine_type"] == "onnxruntime"
    assert params["Det.ocr_version"] == params["Rec.ocr_version"] == "PP-OCRv5"
    assert params["Global.use_cls"] is False
    assert params["EngineConfig.onnxruntime.use_cuda"] is False


def test_native_call_is_off_loop_and_remains_serial_after_cancellation(
    monkeypatch, tmp_path, page
):
    started, release = Event(), Event()
    calls = []
    loads = []

    def engine(image, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return NativeResult()

    def load(model_dir):
        loads.append(1)
        return engine, NativeResult

    monkeypatch.setattr(adapter, "_load_engine", load)
    provider = adapter.RapidOCRProvider(tmp_path)

    async def exercise():
        first = asyncio.create_task(provider.extract_text(page))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(provider.extract_text(page))
            await asyncio.sleep(0.05)
            assert len(calls) == 1
            release.set()
            assert (await asyncio.wait_for(second, 5)).regions == []
        finally:
            release.set()

    asyncio.run(exercise())
    assert len(calls) == 2 and len(loads) == 1
