"""检索查询共用的资料状态与来源范围过滤。"""

from typing import Any

from sqlalchemy import Select

from backend.app.ai.retrieval.base import RetrievalFilters
from backend.app.domain.enums import DocumentStatus
from backend.app.models import Document, DocumentChunk


def apply_retrieval_filters(
    statement: Select[Any],
    scope: RetrievalFilters,
) -> Select[Any]:
    """仅保留就绪资料，并应用课程、知识库和文档范围。"""

    statement = statement.join(
        Document, Document.id == DocumentChunk.document_id,
    ).where(Document.status == DocumentStatus.READY)
    if scope.course_ids:
        statement = statement.where(DocumentChunk.course_id.in_(scope.course_ids))
    if scope.knowledge_base_ids:
        statement = statement.where(
            DocumentChunk.knowledge_base_id.in_(scope.knowledge_base_ids)
        )
    if scope.document_ids:
        statement = statement.where(
            DocumentChunk.document_id.in_(scope.document_ids)
        )
    return statement
