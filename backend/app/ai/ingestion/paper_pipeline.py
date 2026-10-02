"""Single-file page decoding. No persistence, OCR calls or question creation."""

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
    def __init__(self, data: bytes):
        self.data = data
        self.pdf: Any = None
        self.reader: PdfReader | None = None
        self.image: Image.Image | None = None
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
        if self.page_count > 50:
            raise PaperInputError(
                "PAPER_TOO_MANY_PAGES",
                f"试卷实际为 {self.page_count} 页，最多允许 50 页。",
            )
        if self.page_count < 1:
            raise ValueError("PDF has no pages")

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
        for index, original in enumerate(self.reader.pages):
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
def open_paper(data: bytes) -> Iterator[PaperPages]:
    paper = None
    try:
        paper = PaperPages(data)
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
