"""T027 personal page batch: 10 pages, explicit remainder, legacy behaviour kept.

Rendering itself needs ``pypdfium2`` (declared dependency, restored in the
isolated Test venv). These assertions cover the batch/cap budget contract that
must hold regardless of the renderer.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfWriter

from backend.app.ai.ingestion.paper_pipeline import (
    LEGACY_MAX_PAGES,
    PaperInputError,
    open_paper,
)

BATCH = 10
BUDGET = 10 * 1024 * 1024


def blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_personal_batch_processes_ten_pages_and_keeps_the_rest_visible():
    with open_paper(blank_pdf(25), batch_pages=BATCH, decoded_rgba_bytes=BUDGET) as paper:
        assert paper.page_count == 25
        assert paper.processed_through == 10
        assert paper.unprocessed_pages == tuple(range(11, 26))


def test_continuation_requires_an_explicit_start_page():
    with open_paper(
        blank_pdf(25), batch_pages=BATCH, decoded_rgba_bytes=BUDGET, start_page=11
    ) as paper:
        assert paper.processed_through == 20
        assert paper.unprocessed_pages == (21, 22, 23, 24, 25)
    with open_paper(
        blank_pdf(25), batch_pages=BATCH, decoded_rgba_bytes=BUDGET, start_page=21
    ) as paper:
        assert paper.processed_through == 25
        assert paper.unprocessed_pages == ()


def test_out_of_range_start_page_is_refused_not_clamped():
    with (
        pytest.raises(PaperInputError) as error,
        open_paper(blank_pdf(3), batch_pages=BATCH, start_page=9),
    ):
        pass
    assert error.value.code == "PAPER_INVALID_RANGE"


def test_decoded_page_budget_rejects_instead_of_downsampling():
    with (
        open_paper(blank_pdf(1), batch_pages=BATCH, decoded_rgba_bytes=1024) as paper,
        pytest.raises(PaperInputError) as error,
    ):
        paper._check_budget(595, 842)
    assert error.value.code == "PAPER_PAGE_TOO_LARGE"


def test_legacy_paper_consumer_keeps_its_page_cap():
    with (
        pytest.raises(PaperInputError) as error,
        open_paper(blank_pdf(LEGACY_MAX_PAGES + 1)),
    ):
        pass
    assert error.value.code == "PAPER_TOO_MANY_PAGES"
    with open_paper(blank_pdf(LEGACY_MAX_PAGES)) as paper:
        assert paper.processed_through == LEGACY_MAX_PAGES
        assert paper.unprocessed_pages == ()


def test_single_image_page_stays_one_page_needing_ocr():
    output = BytesIO()
    Image.new("RGB", (120, 90), "white").save(output, format="PNG")
    with open_paper(output.getvalue(), batch_pages=BATCH) as paper:
        assert paper.page_count == 1
        assert paper.unprocessed_pages == ()
