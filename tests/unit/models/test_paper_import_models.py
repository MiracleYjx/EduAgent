"""T153 persistent correction/source contract. TCR §10."""
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.domain.enums import DocumentPurpose, DocumentStatus
from backend.app.models import Document, ExtractedQuestion, PaperImport, SourcePage
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)

owned_document = _owned_document


def paper(session, doc, teacher):
    source = Document(
        course_id=doc.course_id, uploaded_by=teacher.id, purpose=DocumentPurpose.PAPER_SOURCE,
        original_filename="original.pdf", file_format="pdf", storage_path="papers/original.pdf",
        status=DocumentStatus.READY,
    )
    session.add(source)
    session.flush()
    imported = PaperImport(
        course_id=doc.course_id, uploaded_by=teacher.id, document=source,
        original_filename="original.pdf", page_count=1,
    )
    session.add(imported)
    session.flush()
    return imported


def test_original_path_is_projection_and_correction_json_survives_reload(owned_document):
    session, doc, teacher, _root = owned_document
    imported = paper(session, doc, teacher)
    page = SourcePage(paper_import_id=imported.id, page_number=1, image_path="papers/page.png", width=1000, height=1400)
    session.add(page)
    session.flush()
    question = ExtractedQuestion(
        paper_import_id=imported.id, source_page_ids=[str(page.id)], extracted_by="TEXT",
        question_number="01", analysis=None, knowledge_points=[], source_regions=None, assets=[],
    )
    session.add(question)
    session.commit()
    identity = question.id
    imported.document.storage_path = "papers/migrated.pdf"
    session.commit()
    session.expire_all()
    fresh = session.get(ExtractedQuestion, identity)
    assert fresh.question_number == "01"
    assert fresh.analysis is None and fresh.source_regions is None and fresh.knowledge_points == []
    assert fresh.source_page_ids == [str(page.id)] and fresh.assets == []
    assert fresh.paper_import.original_file_path == "papers/migrated.pdf"
    assert "original_file_path" not in PaperImport.__table__.c
    assert page.ocr_text is None and page.ocr_confidence is None


@pytest.mark.parametrize("purpose,kb", [("knowledge_base", False), ("paper_source", True)])
def test_document_conditional_knowledge_base_constraint(owned_document, purpose, kb):
    session, doc, teacher, _root = owned_document
    bad = Document(
        course_id=doc.course_id, knowledge_base_id=doc.knowledge_base_id if kb else None,
        uploaded_by=teacher.id, original_filename="bad.pdf", file_format="pdf", purpose=purpose,
    )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(bad)
        session.flush()


def test_page_dimensions_and_corrected_link_constraints(owned_document):
    session, doc, teacher, _root = owned_document
    imported = paper(session, doc, teacher)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(SourcePage(paper_import_id=imported.id, page_number=0, image_path="papers/x.png", width=1, height=1))
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(ExtractedQuestion(paper_import_id=imported.id, source_page_ids=[], extracted_by="TEXT", status="Corrected"))
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(ExtractedQuestion(paper_import_id=uuid4(), source_page_ids=[], extracted_by="TEXT"))
        session.flush()
