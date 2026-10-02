"""T155 已确认的 RapidOCR + ONNX CPU 适配器，无下载或备用 Provider。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import math
from decimal import Decimal
from numbers import Real
from pathlib import Path
from threading import Lock
from typing import Any

from PIL import Image

from .base import BaseOCRProvider, OCRProviderError
from .schemas import OCRProviderInfo, OCRResult

MODEL_ID = "PP-OCRv5-mobile"
# 官方 v3.9.2 模型清单，与 T155 实测使用的权重相同。
MODEL_SHA256 = {
    "ch_PP-OCRv5_det_mobile.onnx": "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    "ch_PP-OCRv5_rec_mobile.onnx": "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}


def _model_paths(model_dir: Path) -> dict[str, Path]:
    paths = {}
    for filename, expected in MODEL_SHA256.items():
        path = model_dir / filename
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise ValueError(f"model checksum mismatch: {filename}")
        paths[filename] = path
    return paths


def _load_engine(model_dir: Path) -> tuple[Any, type]:
    # SDK 只在首次识别时导入；显式 model_path 阻止其默认在线下载。
    sdk = importlib.import_module("rapidocr")
    output_type = importlib.import_module("rapidocr.utils.output").RapidOCROutput
    paths = _model_paths(model_dir)
    params: dict[str, Any] = {
        "Global.use_det": True,
        "Global.use_rec": True,
        "Global.use_cls": False,
        "Global.log_level": "error",
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_cuda": False,
        "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.use_cann": False,
        "EngineConfig.onnxruntime.use_coreml": False,
    }
    for stage, filename in zip(("Det", "Rec", "Cls"), MODEL_SHA256, strict=True):
        params[f"{stage}.engine_type"] = sdk.EngineType.ONNXRUNTIME
        params[f"{stage}.model_type"] = sdk.ModelType.MOBILE
        params[f"{stage}.ocr_version"] = (
            sdk.OCRVersion.PPOCRV4 if stage == "Cls" else sdk.OCRVersion.PPOCRV5
        )
        params[f"{stage}.model_path"] = str(paths[filename])
    # RapidOCR 即使 use_cls=false 仍初始化分类会话，所以也必须预置分类权重。
    return sdk.RapidOCR(params=params), output_type


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise TypeError("native OCR value is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("native OCR value is not finite")
    return number


def _sequence(value: Any) -> list[Any] | tuple[Any, ...]:
    # ndarray 来自 SDK；拒绝空字典/字符串被 len=0 当作合法空区域。
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise TypeError("native OCR field must be an array")
    return value


def _convert_output(
    raw: Any, output_type: type, image_size: tuple[int, int]
) -> OCRResult:
    if not isinstance(raw, output_type):
        raise TypeError("missing or partial RapidOCROutput")
    boxes, texts, scores = (getattr(raw, name) for name in ("boxes", "txts", "scores"))
    # SDK 的正常无检测结果为三个 None；单独检测结果是另一种类型，不可当空页。
    if boxes is None and texts is None and scores is None:
        return OCRResult(text="", regions=[], confidence=None)
    if boxes is None or texts is None or isinstance(texts, (str, bytes)):
        raise ValueError("incomplete OCR fields")
    boxes, texts = _sequence(boxes), _sequence(texts)
    if scores is not None:
        scores = _sequence(scores)
    if len(boxes) != len(texts) or (scores is not None and len(scores) != len(texts)):
        raise ValueError("OCR region/text/confidence counts differ")
    regions = []
    for index, (polygon, text) in enumerate(zip(boxes, texts, strict=True)):
        if not isinstance(text, str) or len(polygon) != 4:
            raise ValueError("invalid OCR text or quadrilateral")
        points = []
        for point in polygon:
            if len(point) != 2:
                raise ValueError("invalid OCR point")
            points.append((_number(point[0]), _number(point[1])))
        xs, ys = zip(*points, strict=True)
        confidence = None
        if scores is not None and scores[index] is not None:
            # 原生比例值，不猜测百分比，也不聚合成 SDK 未提供的总体置信度。
            _number(scores[index])
            confidence = Decimal(str(scores[index]))
        regions.append(
            {
                "bbox": (min(xs), min(ys), max(xs), max(ys)),
                "text": text,
                "confidence": confidence,
            }
        )
    return OCRResult.model_validate(
        {"text": "\n".join(texts), "regions": regions, "confidence": None},
        context={"image_size": image_size},
    )


class RapidOCRProvider(BaseOCRProvider):
    def __init__(self, model_dir: Path) -> None:
        self._model_dir = model_dir.expanduser().resolve()
        self._engine: Any = None
        self._output_type: type | None = None
        self._lock = Lock()
        self._reason: str | None = "模型尚未加载；首次识别时检查可选依赖与预置资源。"

    def describe(self) -> OCRProviderInfo:
        return OCRProviderInfo(
            provider="rapidocr",
            model=MODEL_ID,
            ready=self._engine is not None,
            reason=self._reason,
        )

    async def extract_text(self, image_path: Path) -> OCRResult:
        # 锁留在工作线程内；取消等待不让下一调用与尚未结束的原生推理并发。
        return await asyncio.to_thread(self._extract_sync, image_path)

    def _extract_sync(self, image_path: Path) -> OCRResult:
        with self._lock:
            try:
                source = Image.open(image_path)
                with source:
                    source.load()
                    image = source.convert("RGB")
            except FileNotFoundError as exc:
                raise OCRProviderError(
                    "FILE_MISSING", "输入页图已丢失，请检查原页文件。"
                ) from exc
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise OCRProviderError(
                    "OCR_INPUT_INVALID",
                    "页图无法读取，请检查文件格式、内容和像素尺寸。",
                ) from exc
            with image:
                if self._engine is None:
                    try:
                        engine, output_type = _load_engine(self._model_dir)
                    except Exception as exc:
                        self._reason = (
                            "OCR 推理资源未就绪；请安装 ocr 可选依赖，检查 ONNX CPU "
                            "运行环境及 OCR_MODEL_DIR 中三个预置模型的完整性。"
                        )
                        raise OCRProviderError(
                            "OCR_PROVIDER_NOT_READY", self._reason
                        ) from exc
                    self._output_type = output_type
                    self._reason = None
                    self._engine = engine
                try:
                    # 保留低置信度原生文字，不把低分识别静默过滤成空白页。
                    raw = self._engine(image, text_score=0.0)
                except Exception as exc:
                    raise OCRProviderError(
                        "OCR_CALL_FAILED",
                        "OCR 推理失败，请检查运行环境或原页后重新处理。",
                    ) from exc
                try:
                    assert self._output_type is not None
                    return _convert_output(raw, self._output_type, image.size)
                except Exception as exc:
                    raise OCRProviderError(
                        "OCR_OUTPUT_INVALID",
                        "OCR 输出结构、区域坐标或置信度无效，请核对原页。",
                    ) from exc
