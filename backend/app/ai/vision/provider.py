"""图片应用层通过既有 BaseLLMProvider 完成结构化理解，不持有供应商 SDK。"""
from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from backend.app.ai.llm.base import BaseLLMProvider, describe_llm_provider
from backend.app.ai.llm.factory import LLMProviderError, create_llm_provider
from backend.app.core.config import AppSettings
from backend.app.core.retry_policy import (
    ProviderCallError,
    ProviderExecutionError,
    classify_provider_exception,
)
from backend.app.schemas.image_assessment import Provenance

from .base import (
    MAX_REQUEST_BYTES,
    ProviderImage,
    VisionFailure,
    VisionImage,
    VisionResult,
)

VISION_PROMPT_VERSION = "vision-conditions-v1"
_INSTRUCTION = """仅依据本次全部题图和真实文字上下文识别图示、表格及必要条件。
输出符合所给 JSON Schema 的 JSON 对象，不输出 Markdown，不补写题图未给出的答案、数值或关系。
图片 image_index 从 1 开始，按下列输入顺序对应；bbox/evidence_region.bbox 为该资产原图
左上/右下像素 (x0,y0,x1,y1)，无法可靠定位保持 null，不使用归一化坐标。
无法辨认/矛盾/缺失条件写入 unresolved_issues，未知图片归属 image_indices=null；
有问题 requires_manual_review=true。没有问题的 false 也不代替真实教师核对。
provenance 必须为 null，不生成资产、文件、教师或 Provider 身份。"""


def create_vision_provider(settings: AppSettings) -> BaseLLMProvider:
    """同 Provider 的独立实例；显式图像模型不会更换既有文字实例/配置。"""
    if not settings.vision_model:
        raise VisionFailure(
            "VISION_PROVIDER_NOT_READY", "未显式配置 VISION_MODEL，请先完成图像模型配置或使用人工核对。",
            stage="configuration",
        )
    if settings.llm_provider != "deepseek":
        raise VisionFailure(
            "VISION_PROVIDER_NOT_READY", "当前已配置 Provider 尚无本批已确认的独立图像模型适配。",
            stage="configuration",
        )
    try:
        vision_settings = settings.model_copy(update={"deepseek_model": settings.vision_model})
        return create_llm_provider(vision_settings)
    except (LLMProviderError, ValueError, TypeError, ImportError) as error:
        # 工厂错误不携带密钥、端点或 SDK request；此时没有发起模型调用。
        raise VisionFailure(
            "VISION_PROVIDER_NOT_READY", "图像 Provider 实例未能初始化。",
            stage="configuration", cause=type(error).__name__,
        ) from None


def _provenance(provider: BaseLLMProvider) -> Provenance:
    actual = describe_llm_provider(provider, prompt_version=VISION_PROMPT_VERSION)
    return Provenance(
        provider_name=None if actual["provider"] == "unknown" else actual["provider"],
        model=None if actual["model"] == "unknown" else actual["model"],
        model_version=None, prompt_version=VISION_PROMPT_VERSION,
    )


class ProviderVisionUnderstanding:
    """无自动换供应商/文本回退；合法模型输出仍须教师核对。"""

    def __init__(self, provider: BaseLLMProvider) -> None:
        self.provider = provider
        self.call_provenance: Provenance | None = None

    async def understand(
        self, *, context: Mapping[str, Any], images: Sequence[VisionImage], task: str,
    ) -> VisionResult:
        self.call_provenance = None
        if not self.provider.supports_vision():
            raise VisionFailure(
                "VISION_NOT_SUPPORTED", "当前实际配置模型与适配器未声明图像能力。",
                stage="capability",
            )
        inputs = list(images)
        if not 1 <= len(inputs) <= 5:
            raise VisionFailure(
                "VISION_IMAGE_TRANSPORT_UNAVAILABLE", "本轮图片输入须包含当前整组 1–5 张题图。",
                stage="image_transport",
            )
        if not task.strip():
            raise VisionFailure("VISION_OUTPUT_INVALID", "图片理解任务不能为空。", stage="input")
        for image in inputs:
            actual = VisionImage.from_bytes(image.data)
            if (actual.width, actual.height, actual.mime_type) != (image.width, image.height, image.mime_type):
                raise VisionFailure(
                    "VISION_IMAGE_TRANSPORT_UNAVAILABLE", "输入图像尺寸/格式与实际 bytes 不一致。",
                    stage="image_transport",
                )
        try:
            text = json.dumps({"task": task, "context": dict(context)}, ensure_ascii=False, allow_nan=False, default=to_jsonable_python)
        except (TypeError, ValueError):
            raise VisionFailure("VISION_OUTPUT_INVALID", "真实文字上下文无法按 JSON 传输。", stage="input") from None
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for index, image in enumerate(inputs, 1):
            parts.extend([
                {"type": "text", "text": f"第 {index} 张图片，原图尺寸 {image.width}×{image.height} 像素。"},
                {"type": "image", "image": ProviderImage(
                    encoding="base64", value=base64.b64encode(image.data).decode("ascii"), mime_type=image.mime_type,
                )},
            ])
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _INSTRUCTION + "\nJSON Schema:\n" + json.dumps(VisionResult.model_json_schema(), ensure_ascii=False)},
            {"role": "user", "content": parts},
        ]
        serialized = json.dumps(messages, ensure_ascii=False, default=lambda value: value.model_dump(), allow_nan=False)
        if len(serialized.encode("utf-8")) > MAX_REQUEST_BYTES - 1024:
            raise VisionFailure(
                "VISION_IMAGE_TRANSPORT_UNAVAILABLE", "本轮图像与文字超过适配器请求体限制。",
                stage="image_transport",
            )
        provenance = _provenance(self.provider)
        self.call_provenance = provenance
        try:
            output = await self.provider.generate_structured(messages=messages, schema=VisionResult)
        except asyncio.CancelledError:
            # 上层在持久保存真实失败后重新抛出取消，不把取消变成普通成功返回。
            raise
        except ValidationError:
            raise VisionFailure(
                "VISION_OUTPUT_INVALID", "模型输出未通过结构化校验。",
                stage="output", cause="ValidationError", provenance=provenance,
            ) from None
        except Exception as error:  # noqa: BLE001 — Provider 边界保存真实失败，不暴露 SDK request。
            if isinstance(error, ProviderExecutionError):
                code, message, retryable = error.info.code, error.info.message, error.info.retryable
            elif isinstance(error, ProviderCallError):
                code, message, retryable = error.code, error.safe_message, error.retryable
            else:
                safe = classify_provider_exception(error)
                code, message, retryable = safe.code, safe.safe_message, safe.retryable
            outcome_code = "VISION_OUTPUT_INVALID" if code in {"StructuredOutputFailed", "ProviderEmptyResponse"} else "VISION_CALL_FAILED"
            raise VisionFailure(
                outcome_code, message, stage="output" if outcome_code == "VISION_OUTPUT_INVALID" else "call",
                cause=code, retryable=retryable, provenance=provenance,
            ) from None
        try:
            result = VisionResult.model_validate(output.model_dump())
            if result.provenance is not None:
                raise ValueError("模型不能生成实际调用来源。")
            result.validate_images(inputs)
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise VisionFailure(
                "VISION_OUTPUT_INVALID", "模型结果与本轮实际图像或业务身份边界不一致。",
                stage="output", cause="ResultInputMismatch", provenance=provenance,
            ) from None
        result.provenance = provenance
        return result


__all__ = ["VISION_PROMPT_VERSION", "ProviderVisionUnderstanding", "create_vision_provider"]
