"""A4 local material OCR preserves source facts and explicit review boundaries."""

from backend.app.services.material_ocr import ocr_candidates, source_digest


def test_ocr_candidates_keep_scanned_pages_and_skip_real_pdf_text():
    facts = {"blocks": [
        {"type": "text", "anchor": "page-1", "page": 1, "text": "real text"},
        {"type": "image", "anchor": "page-1", "page": 1, "sha256": "a" * 64},
        {"type": "image", "anchor": "page-2", "page": 2, "sha256": "b" * 64},
    ]}
    assert [item["anchor"] for item in ocr_candidates(facts)] == ["page-2"]


def test_ocr_retry_does_not_repeat_completed_native_pages():
    facts = {"blocks": [
        {"type": "image", "anchor": "page-1", "page": 1, "sha256": "a" * 64},
        {"type": "text", "anchor": "page-1:ocr", "page": 1,
         "text": "actual OCR", "ocr_source_anchor": "page-1"},
        {"type": "image", "anchor": "page-2", "page": 2, "sha256": "b" * 64},
    ]}
    assert [item["anchor"] for item in ocr_candidates(facts)] == ["page-2"]


def test_preview_bookkeeping_does_not_hide_source_changes():
    original = {"blocks": [{"type": "image", "anchor": "page-1", "sha256": "a" * 64}]}
    assert source_digest(original) == source_digest({**original, "local_ocr": {"state": "running"}})
    changed = {"blocks": [{"type": "image", "anchor": "page-1", "sha256": "b" * 64}]}
    assert source_digest(original) != source_digest(changed)
