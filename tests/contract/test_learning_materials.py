"""T026 material import contract: formats, purpose split, limits and retrieval scope.

The assertions here are format/policy facts that do not need a database. Real
PostgreSQL behaviour (versions, history, retrieval) lives in
``tests/integration/test_learning_material_versions.py``.

Synthetic DOCX fixtures are written with the standard library only: the project
declares neither ``python-docx`` nor ``lxml``, and this batch has no new
dependency approval.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image, ImageDraw

from backend.app.ai.ingestion.parsers import (
    DEFAULT_MATERIAL_LIMITS,
    DocumentParseError,
    parse_document,
    resolve_docx_anchor,
)

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
_DOCX_DECL = (
    'xmlns:w="' + NS["w"] + '" xmlns:m="' + NS["m"] + '" xmlns:a="'
    + NS["a"] + '" xmlns:r="' + NS["r"] + '" xmlns:wp="' + NS["wp"] + '"'
)


def _limit(code: str):
    """Return the parser module or fail with an actionable red-light message."""

    module = "backend.app.ai.ingestion.parsers"
    spec = importlib.util.find_spec(module)
    if spec is None:
        pytest.fail("T027_IMPLEMENTATION_REQUIRED: parsers", pytrace=False)
    return code


def png_bytes(label: str = "IMG", size: tuple[int, int] = (160, 60)) -> bytes:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((8, 15), label, fill="steelblue")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (200, 120), "white").save(output, format="JPEG")
    return output.getvalue()


def sample_docx(*, unsupported: bool = False, images: int = 1) -> bytes:
    """Synthetic DOCX with real source anchors plus retained OOXML objects.

    Body order is intentionally fixed: text, inline image, text, formula, text,
    table, text. Floating placement and OMML share the same finite walker.
    """

    media = png_bytes()
    runs = ['<w:r><w:t xml:space="preserve">BEFORE IMAGE </w:t></w:r>']
    relationships = []
    for index in range(images):
        relationships.append(
            f'<Relationship Id="rIdImg{index + 1}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/image" Target="media/image{index + 1}.png"/>'
        )
        anchor = (
            f'<w:r><w:drawing><wp:inline><a:graphic><a:graphicData><a:blip r:embed="rIdImg{index + 1}"/>'
            "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
            if index == 0
            else f'<w:r><w:drawing><wp:anchor><a:graphic><a:graphicData>'
            f'<a:blip r:embed="rIdImg{index + 1}"/></a:graphicData></a:graphic>'
            "</wp:anchor></w:drawing></w:r>"
        )
        runs.append(anchor)
        runs.append(
            f'<w:r><w:t xml:space="preserve"> AFTER IMAGE {index + 1} </w:t></w:r>'
        )
    runs.append(
        '<m:oMath><m:r><m:t>x+1</m:t></m:r></m:oMath>'
    )
    runs.append('<w:r><w:t xml:space="preserve"> AFTER MATH </w:t></w:r>')
    table = (
        "<w:tbl>"
        "<w:tr><w:tc><w:p><w:r><w:t>OPTION</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>VALUE</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>7</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:tbl>"
    )
    tail = '<w:p><w:r><w:t>EXPLANATION END</w:t></w:r></w:p>'
    if unsupported:
        tail = (
            '<w:p><w:r><w:t>EXPLANATION END</w:t></w:r>'
            "<w:pict><w:unknownObject/></w:pict></w:p>"
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_DOCX_DECL}><w:body>"
        f"<w:p>{''.join(runs)}</w:p>"
        f"{table}{tail}"
        "</w:body></w:document>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(relationships)}</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/package/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", rels)
        for index in range(images):
            archive.writestr(f"word/media/image{index + 1}.png", media)
    return output.getvalue()


def test_all_approved_first_release_formats_are_parsed():
    text = "第三章 函数\n\n定义：给定定义域内的每个输入，都有唯一输出。".encode()
    markdown = "# 章节\n\n公式 $y=f(x)$ 的说明。".encode()
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    pdf = BytesIO()
    writer.write(pdf)

    for filename, data, expected in (
        ("notes.txt", text, "txt"),
        ("notes.md", markdown, "markdown"),
        ("blank.pdf", pdf.getvalue(), "pdf"),
        ("scan.png", png_bytes(), "png"),
        ("photo.jpg", jpeg_bytes(), "jpeg"),
        ("教材.docx", sample_docx(), "docx"),
    ):
        parsed = parse_document(filename, data)
        assert parsed.file_format == expected, filename


def test_unsupported_epub_stays_explicitly_rejected():
    with pytest.raises(DocumentParseError) as error:
        parse_document("book.epub", b"PK\x03\x04not-accepted")
    assert error.value.error_code == "DOCUMENT_UNSUPPORTED_FORMAT"
    assert error.value.retryable is False


def test_docx_keeps_reading_order_anchors_objects_and_original_media():
    data = sample_docx(images=2)
    parsed = parse_document("教材.docx", data)
    kinds = [block.kind for block in parsed.blocks]
    assert kinds == [
        "text",
        "image",
        "text",
        "image",
        "text",
        "formula",
        "text",
        "table",
        "text",
    ]
    assert next(block.text for block in parsed.blocks if block.kind == "text") == (
        "BEFORE IMAGE"
    )
    assert [b.placement for b in parsed.blocks if b.kind == "image"] == [
        "inline",
        "floating",
    ]
    table = next(block for block in parsed.blocks if block.kind == "table")
    assert table.rows == (("OPTION", "VALUE"), ("A", "7"))
    formula = next(block for block in parsed.blocks if block.kind == "formula")
    assert "x+1" in (formula.mathml or "")
    assert len(parsed.blocks) == len({block.anchor for block in parsed.blocks})
    from xml.etree import ElementTree

    with ZipFile(BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        for block in parsed.blocks:
            assert block.anchor.startswith("/w:document/")
            assert resolve_docx_anchor(root, block.anchor) is not None
        for block in parsed.blocks:
            if block.kind != "image":
                continue
            assert block.sha256 == hashlib.sha256(block.image or b"").hexdigest()
            assert block.media_type == "image/png"
        assert parsed.issues == ()


def test_unsupported_docx_object_is_retained_with_reason_not_dropped():
    parsed = parse_document("教材.docx", sample_docx(unsupported=True))
    assert parsed.issues == ("OOXML_UNSUPPORTED_PICT",)
    retained = [b for b in parsed.blocks if b.kind == "original"]
    assert len(retained) == 1
    assert retained[0].reason == "OOXML_UNSUPPORTED_PICT"
    assert "unknownObject" in (retained[0].original or "")


def test_image_material_is_a_real_page_without_inventing_text():
    parsed = parse_document("scan.png", png_bytes())
    assert parsed.requires_ocr is True
    assert parsed.text == ""
    assert [block.kind for block in parsed.blocks] == ["image"]
    assert parsed.blocks[0].page == 1
    assert parsed.issues == ()


def test_approved_limits_reject_instead_of_truncating():
    assert DEFAULT_MATERIAL_LIMITS.upload_bytes == 20 * 1024 * 1024
    assert DEFAULT_MATERIAL_LIMITS.docx_expanded_bytes == 20 * 1024 * 1024
    assert DEFAULT_MATERIAL_LIMITS.docx_image_occurrences == 8
    with pytest.raises(DocumentParseError) as oversized:
        parse_document("big.txt", b"a" * (DEFAULT_MATERIAL_LIMITS.upload_bytes + 1))
    assert oversized.value.error_code == "DOCUMENT_SIZE_LIMIT"
    with pytest.raises(DocumentParseError) as images:
        parse_document("教材.docx", sample_docx(images=9))
    assert images.value.error_code == "DOCUMENT_IMAGE_LIMIT"
    with pytest.raises(DocumentParseError) as expanded:
        parse_document(
            "教材.docx",
            sample_docx(),
            max_expanded_bytes=1024,
        )
    assert expanded.value.error_code == "DOCUMENT_EXPANDED_LIMIT"


def test_corrupted_and_empty_inputs_never_become_content():
    with pytest.raises(DocumentParseError) as empty:
        parse_document("empty.txt", b"")
    assert empty.value.error_code == "DOCUMENT_EMPTY"
    for name in ("broken.docx", "broken.pdf", "broken.png"):
        with pytest.raises(DocumentParseError) as broken:
            parse_document(name, b"not a real document")
        assert broken.value.error_code in {
            "DOCUMENT_CORRUPTED",
            "DOCUMENT_PARSE_FAILED",
        }, name


def test_learning_limits_and_parser_limits_share_one_policy():
    from backend.app.services.learning_operations import LearningLimits

    approved = LearningLimits()
    assert DEFAULT_MATERIAL_LIMITS.upload_bytes == approved.upload_bytes
    assert DEFAULT_MATERIAL_LIMITS.docx_expanded_bytes == approved.docx_expanded_bytes
    assert (
        DEFAULT_MATERIAL_LIMITS.docx_image_occurrences
        == approved.docx_image_occurrences
    )
    assert DEFAULT_MATERIAL_LIMITS.decoded_rgba_bytes == approved.decoded_rgba_bytes
    assert DEFAULT_MATERIAL_LIMITS.pdf_batch_pages == approved.pdf_batch_pages


def test_material_purpose_split_is_explicit_and_bounded():
    from backend.app.schemas.personal_learning import MaterialPurpose, MaterialUpload

    assert {p.value for p in MaterialPurpose} == {"study_material", "exam_paper"}
    with pytest.raises(ValueError):
        MaterialUpload(purpose="textbook")
    paper = MaterialUpload(purpose="exam_paper")
    study = MaterialUpload(purpose="study_material")
    assert paper.builds_teaching_index is False
    assert study.builds_teaching_index is True


def test_optional_metadata_is_optional_and_never_guessed():
    from backend.app.schemas.personal_learning import MaterialUpload

    empty = MaterialUpload(purpose="study_material")
    assert (empty.year, empty.publisher, empty.isbn, empty.exam_name) == (
        None,
        None,
        None,
        None,
    )
    assert empty.optional_metadata() == {}
    filled = MaterialUpload(
        purpose="study_material",
        year=2024,
        publisher="某某出版社",
        isbn="9787000000000",
    )
    assert filled.year == 2024 and filled.publisher == "某某出版社"
    assert filled.optional_metadata() == {
        "year": 2024,
        "publisher": "某某出版社",
        "isbn": "9787000000000",
    }
    with pytest.raises(ValueError):
        MaterialUpload(purpose="study_material", year=1200)


def test_no_network_acquisition_path_exists_for_materials():
    module = "backend.app.services.learning_material_service"
    if importlib.util.find_spec(module) is None:
        pytest.fail("T028_IMPLEMENTATION_REQUIRED: learning_material_service", pytrace=False)
    import backend.app.services.learning_material_service as service

    source = inspect.getsource(service)
    for forbidden in ("httpx", "requests.", "urllib.request", "aiohttp", "urlopen"):
        assert forbidden not in source, forbidden


def test_retrieval_filters_accept_subject_scope_and_reject_unknown_keys():
    from backend.app.ai.retrieval.base import RetrievalInputError, resolve_filters

    subject = uuid4()
    version = uuid4()
    scope = resolve_filters({"subject_ids": (subject,), "material_version_ids": (version,)})
    assert scope.subject_ids == (subject,)
    assert scope.material_version_ids == (version,)
    assert scope.is_empty is False
    with pytest.raises(RetrievalInputError):
        resolve_filters({"course": (subject,)})


def test_personal_chunk_result_keeps_subject_when_course_is_absent():
    from backend.app.ai.retrieval.base import RetrievedChunk

    subject = uuid4()
    chunk = SimpleNamespace(
        id=uuid4(),
        course_id=None,
        subject_id=subject,
        document_id=uuid4(),
        content="真实片段",
        chunk_metadata={"location": "第 2 页"},
    )
    result = RetrievedChunk.from_document_chunk(chunk)
    assert result.subject_id == str(subject)
    assert result.course_id == ""
    assert result.as_dict()["subject_id"] == str(subject)
