from __future__ import annotations

from functools import lru_cache

from backend.app.core.config import AppSettings, get_settings

from .base import BaseOCRProvider, OCRProviderError


def create_ocr_provider(settings: AppSettings | None = None) -> BaseOCRProvider:
    """只在调用方确实需要 OCR 时调用；不在 v1 启动路径中构造。"""
    settings = settings or get_settings()
    if not settings.ocr_enabled:
        raise OCRProviderError(
            "OCR_PROVIDER_NOT_READY",
            "OCR 未启用；扫描页需要配置 OCR_ENABLED 后重新处理。",
        )
    if settings.ocr_provider != "rapidocr":
        raise OCRProviderError(
            "OCR_PROVIDER_NOT_READY",
            "请显式配置 OCR_PROVIDER=rapidocr；不自动切换适配器。",
        )
    from .rapidocr import MODEL_ID, RapidOCRProvider

    if settings.ocr_model not in (None, MODEL_ID):
        raise OCRProviderError(
            "OCR_PROVIDER_NOT_READY", "当前适配器只支持 OCR_MODEL=PP-OCRv5-mobile。"
        )
    if settings.ocr_model_dir is None:
        raise OCRProviderError(
            "OCR_PROVIDER_NOT_READY",
            "请配置 OCR_MODEL_DIR 并预置已确认的三个 ONNX 模型。",
        )
    return RapidOCRProvider(settings.ocr_model_dir)


@lru_cache(maxsize=1)
def get_ocr_provider() -> BaseOCRProvider:
    """进程内复用推理会话；更换配置后重启或显式清理缓存。"""
    return create_ocr_provider()


def reset_ocr_provider_cache() -> None:
    get_ocr_provider.cache_clear()
