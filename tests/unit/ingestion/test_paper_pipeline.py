"""T157 real PDF rendering, page selection and malformed inputs. TCR §13."""

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from backend.app.ai.ingestion.paper_pipeline import PaperInputError, open_paper

SAMPLES = (
    Path(__file__).resolve().parents[3] / "benchmark/corpus/v2-draft-20261001/inputs"
)


def test_text_and_mixed_pages_use_real_content_and_render():
    for name, expected in [
        ("paper_text.pdf", [False]),
        ("paper_mixed.pdf", [False, True]),
        ("paper_cross_page.pdf", [False, False]),
    ]:
        with open_paper((SAMPLES / name).read_bytes()) as paper:
            pages = list(paper.pages())
        assert [p.needs_ocr for p in pages] == expected
        for page in pages:
            with Image.open(BytesIO(page.png)) as image:
                assert image.size == (page.width, page.height)
                assert image.format == "PNG"
            if not page.needs_ocr:
                assert page.text


def test_scan_requires_ocr_and_50_pages_are_not_truncated():
    with open_paper((SAMPLES / "paper_scan.pdf").read_bytes()) as paper:
        assert next(paper.pages()).needs_ocr
    with open_paper((SAMPLES / "workload_50_pages.pdf").read_bytes()) as paper:
        assert paper.page_count == 50
    with (
        pytest.raises(PaperInputError) as exc,
        open_paper((SAMPLES / "workload_51_pages.pdf").read_bytes()),
    ):
        pass
    assert exc.value.code == "PAPER_TOO_MANY_PAGES"
    assert "51" in str(exc.value)


@pytest.mark.parametrize("content", [b"", b"bad", b"%PDF-1.7 bad data"])
def test_invalid_input_never_becomes_empty_success(content):
    with pytest.raises(PaperInputError) as exc, open_paper(content):
        pass
    assert exc.value.code == "PAPER_PARSE_FAILED"


def test_image_is_single_stored_page_and_no_filename_trust():
    output = BytesIO()
    Image.new("RGB", (240, 320), "white").save(output, "JPEG")
    with open_paper(output.getvalue()) as paper:
        page = next(paper.pages())
        assert paper.page_count == 1 and paper.file_format == "jpeg"
        assert page.needs_ocr and (page.width, page.height) == (240, 320)
