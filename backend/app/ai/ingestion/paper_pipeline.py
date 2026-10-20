"""Single-file page decoding. No persistence, OCR calls or question creation.

Two callers share this module:

- the legacy paper-import path, which keeps its historical 50-page refusal and
  full-page rendering defaults (T034 owns its semantics);
- the personal learning path (T027), which passes the approved L02/L03 values:
  one batch of at most 10 pages, a 10 MiB decoded-page budget, and an explicit
  starting page.

Neither caller silently truncates: a batch exposes exactly how far it processed
and which pages remain, so continuing is a user action, not an automatic
background retry.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from threading import RLock
from typing import Any

from PIL import Image
from pypdf import PdfReader

from backend.app.ai.ingestion.parsers import PdfDocumentParser

# PDFium is not thread safe, even for separate documents.
_PDF_LOCK = RLock()
_DPI = 150
#: 旧试卷导入路径的历史页数上限；新个人路径使用自己的批次参数。
LEGACY_MAX_PAGES = 50


class PaperInputError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    png: bytes
    width: int
    height: int
    text: str
    needs_ocr: bool


class PaperPages:
    def __init__(
        self,
        data: bytes,
        *,
        batch_pages: int | None = None,
        decoded_rgba_bytes: int | None = None,
        start_page: int = 1,
    ):
        self.data = data
        self.pdf: Any = None
        self.reader: PdfReader | None = None
        self.image: Image.Image | None = None
        self.batch_pages = batch_pages
        self.decoded_rgba_bytes = decoded_rgba_bytes
        self.start_page = start_page
        if batch_pages is not None and (batch_pages <= 0 or start_page <= 0):
            raise PaperInputError("PAPER_INVALID_RANGE", "页码范围必须从 1 开始。")
        if data.startswith(b"%PDF-"):
            self.file_format = "pdf"
            self.reader = PdfReader(BytesIO(data), strict=True)
            if self.reader.is_encrypted:
                raise ValueError("encrypted PDF is not supported")
            self.page_count = len(self.reader.pages)
        else:
            with Image.open(BytesIO(data)) as source:
                if (
                    source.format not in {"PNG", "JPEG"}
                    or getattr(source, "n_frames", 1) != 1
                ):
                    raise ValueError("expected one PNG/JPEG page")
                self.file_format = source.format.lower()
                source.load()
                self.image = source.convert("RGB")
            self.page_count = 1
        if batch_pages is None and self.page_count > LEGACY_MAX_PAGES:
            raise PaperInputError(
                "PAPER_TOO_MANY_PAGES",
                f"试卷实际为 {self.page_count} 页，最多允许 {LEGACY_MAX_PAGES} 页。",
            )
        if self.page_count < 1:
            raise ValueError("PDF has no pages")
        if start_page > self.page_count:
            raise PaperInputError("PAPER_INVALID_RANGE", "起始页超出实际页数。")
        if batch_pages is not None:
            self.processed_through = min(
                self.page_count, start_page - 1 + batch_pages
            )
        else:
            self.processed_through = self.page_count

    @property
    def unprocessed_pages(self) -> tuple[int, ...]:
        """Pages this batch deliberately did not process; still in the original."""

        return tuple(range(self.processed_through + 1, self.page_count + 1))

    def _check_budget(self, width: float, height: float) -> None:
        if self.decoded_rgba_bytes is None:
            return
        scale = _DPI / 72
        pixels = int(width * scale) * int(height * scale)
        if pixels <= 0 or pixels * 4 > self.decoded_rgba_bytes:
            raise PaperInputError(
                "PAPER_PAGE_TOO_LARGE",
                "单页解码超过已批准的像素预算，请缩小或拆分该页。",
            )

    def pages(self) -> Iterator[RenderedPage]:
        if self.image is not None:
            output = BytesIO()
            self.image.save(output, "PNG")
            yield RenderedPage(1, output.getvalue(), *self.image.size, "", True)
            return
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
        from pypdfium2 import raw

        with _PDF_LOCK:
            self.pdf = pdfium.PdfDocument(self.data)
            if len(self.pdf) != self.page_count:
                raise PaperInputError(
                    "PAPER_PARSE_FAILED", "PDF 解析器的实际页数不一致。"
                )
        assert self.reader is not None
        for index in range(self.start_page - 1, self.processed_through):
            original = self.reader.pages[index]
            text = PdfDocumentParser._extract_page_text(original, index + 1)
            with _PDF_LOCK:
                page = self.pdf[index]
                try:
                    width, height = page.get_size()
                    area = width * height
                    if area <= 0 or area * (_DPI / 72) ** 2 > (
                        Image.MAX_IMAGE_PIXELS or 89478485
                    ):
                        raise ValueError("page dimensions exceed supported image size")
                    self._check_budget(width, height)
                    # A large scanned region with only a header/text fragment still needs OCR.
                    scanned = False
                    for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE]):
                        x0, y0, x1, y1 = obj.get_bounds()
                        if abs((x1 - x0) * (y1 - y0)) >= area * 0.5:
                            scanned = True
                    bitmap = page.render(scale=_DPI / 72)
                    try:
                        image = bitmap.to_pil()
                        try:
                            output = BytesIO()
                            image.save(output, "PNG")
                            size = image.size
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            reliable = (
                bool(text.strip())
                and any(c.isalnum() for c in text)
                and not any(c in text for c in ("\x00", "\ufffd"))
                and not scanned
            )
            yield RenderedPage(
                index + 1,
                output.getvalue(),
                size[0],
                size[1],
                text if reliable else "",
                not reliable,
            )

    def close(self) -> None:
        if self.pdf is not None:
            with _PDF_LOCK:
                self.pdf.close()
        if self.image is not None:
            self.image.close()


@contextmanager
def open_paper(
    data: bytes,
    *,
    batch_pages: int | None = None,
    decoded_rgba_bytes: int | None = None,
    start_page: int = 1,
) -> Iterator[PaperPages]:
    paper = None
    try:
        paper = PaperPages(
            data,
            batch_pages=batch_pages,
            decoded_rgba_bytes=decoded_rgba_bytes,
            start_page=start_page,
        )
    except PaperInputError:
        raise
    except Exception as exc:
        raise PaperInputError(
            "PAPER_PARSE_FAILED", "原卷无法解析为有效 PDF 或单页 PNG/JPEG。"
        ) from exc
    try:
        yield paper
    finally:
        paper.close()


__all__ = [
    "LEGACY_MAX_PAGES",
    "PaperInputError",
    "PaperPages",
    "RenderedPage",
    "open_paper",
]
