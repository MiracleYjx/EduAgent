"""T153 purpose isolation and approved analysis guard. TCR §10."""
from uuid import UUID

import pytest

from backend.app.domain.enums import QuestionType
from backend.app.models import Question
from backend.app.services.knowledge_base_service import (
    DocumentNotFoundError,
    KnowledgeBaseService,
)
from backend.app.services.question_service import (
    QuestionApprovedImmutableError,
    QuestionService,
)
from tests.support.question_validation_fixtures import persist_current_semantic_pass
from tests.unit.models.test_paper_import_models import paper
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)

owned_document = _owned_document


def test_paper_source_is_not_knowledge_material_or_ingestion(owned_document):
    session, doc, teacher, _root = owned_document
    imported = paper(session, doc, teacher)
    session.commit()
    service = KnowledgeBaseService(session)
    assert [item.id for item in service.list_documents(course_id=doc.course_id)] == [str(doc.id)]
    with pytest.raises(DocumentNotFoundError):
        service.ingest_document(imported.document_id, teacher_id=teacher.id, content=b"not knowledge")
    assert imported.document.status.value == "Ready"


def test_manual_source_analysis_clear_and_real_approval_time(owned_document):
    session, doc, teacher, _root = owned_document
    service = QuestionService(session)
    created = service.create_question(
        doc.course_id,
        QuestionType.SHORT_ANSWER,
        "问题",
        score="2.00",
        created_by=teacher.id,
        analysis="真实解析",
        reference_answer="受控题干的参考答案",
        scoring_rubric="说明参考答案的一个要点计2分。",
    )
    question = session.get(Question, UUID(created.id))
    assert created.source_type.value == "manual"
    assert created.analysis == "真实解析" and created.frozen_at is None
    service.update_question(created.id, difficulty="easy", teacher_id=teacher.id)
    assert question.analysis == "真实解析"
    service.update_question_status(created.id, "Pending Review", teacher_id=teacher.id)
    persist_current_semantic_pass(session, created.id, teacher.id)
    approved = service.update_question_status(
        created.id, "Approved", teacher_id=teacher.id
    )
    assert approved.frozen_at is not None
    with pytest.raises(QuestionApprovedImmutableError):
        service.update_question(created.id, analysis=None, teacher_id=teacher.id)
    revised = service.update_question_status(
        created.id,
        "Needs Revision",
        teacher_id=teacher.id,
        revision_comment="受控教师记录：重新核对解析内容。",
    )
    assert revised.frozen_at is None
    assert (
        service.update_question(
            created.id, analysis=None, teacher_id=teacher.id
        ).analysis
        is None
    )
