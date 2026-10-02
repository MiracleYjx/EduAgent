"""可选 OCR 推理边界；导入此包不会加载 OCR SDK。"""

from .base import BaseOCRProvider, OCRProviderError
from .factory import create_ocr_provider, get_ocr_provider, reset_ocr_provider_cache
from .schemas import OCRProviderInfo, OCRRegion, OCRResult

__all__ = [
    "BaseOCRProvider",
    "OCRProviderError",
    "OCRProviderInfo",
    "OCRRegion",
    "OCRResult",
    "create_ocr_provider",
    "get_ocr_provider",
    "reset_ocr_provider_cache",
]
