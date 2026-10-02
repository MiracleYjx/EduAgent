from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from .schemas import OCRProviderInfo, OCRResult

OCRErrorCode = Literal[
    "OCR_PROVIDER_NOT_READY",
    "FILE_MISSING",
    "OCR_INPUT_INVALID",
    "OCR_CALL_FAILED",
    "OCR_OUTPUT_INVALID",
]


class OCRProviderError(RuntimeError):
    """公开信息脱敏；真实底层错误通过 __cause__ 留给内部诊断。"""

    def __init__(self, code: OCRErrorCode, message: str) -> None:
        self.code = self.error_code = code
        self.message = self.detail = message
        super().__init__(f"{code}：{message}")


class BaseOCRProvider(ABC):
    @abstractmethod
    async def extract_text(self, image_path: Path) -> OCRResult:
        """只处理调用方已授权的单页图；不写导入状态或题目。"""
        raise NotImplementedError

    @abstractmethod
    def describe(self) -> OCRProviderInfo:
        raise NotImplementedError
