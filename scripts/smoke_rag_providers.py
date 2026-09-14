"""使用真实本地 Embedding、PostgreSQL 和 LLM Rerank 验证完整课程摄取链路。

先完成模型下载，再设置 HF_HUB_OFFLINE=1 运行本脚本；不使用替身，失败也保存记录。
本次创建的课程与账号在结束时清理，报告保留来源与分数供复核。

TCR（2026-09-14，H05）：普通契约测试使用替身，无法证明运行依赖就绪；本脚本覆盖
真实 Ready 持久化、向量与全文字段数量、来源追踪、离线 Embedding 和实际 LLM 分数。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.embedding.factory import create_embedding_provider
from backend.app.ai.ingestion.service import IngestionService
from backend.app.ai.retrieval.base import RetrievalFilters, RetrievalQuery
from backend.app.ai.retrieval.hybrid_search import HybridSearchRetriever
from backend.app.ai.retrieval.reranker import build_reranker
from backend.app.core.config import get_settings
from backend.app.core.database import create_database_engine
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import Course, DocumentChunk, Role, User
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService
from backend.app.services.knowledge_base_service import KnowledgeBaseService


def main() -> int:
    """逐阶段验证真实调用，任何失败均保留原状态与可公开的错误信息。"""

    parser = argparse.ArgumentParser(description="真实 RAG Provider 冒烟验证")
    parser.add_argument(
        "--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    args = parser.parse_args()
    lesson = Path(__file__).resolve().parents[1] / "docs/rag-smoke-course.txt"
    output = (
        lesson.parents[1] / "benchmark/results" / f"provider_{args.run_id}_smoke.json"
    )
    record: dict[str, Any] = {
        "run_id": args.run_id,
        "run_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "source_file": "docs/rag-smoke-course.txt",
        "embedding_offline": os.environ.get("HF_HUB_OFFLINE") == "1",
        "sentence_transformers_version": version("sentence-transformers"),
        "stages": {},
    }
    settings = get_settings()
    engine = create_database_engine(settings)
    stage = "database"
    started = time.perf_counter()
    with Session(engine, expire_on_commit=False) as session:
        teacher: User | None = None
        course: Course | None = None
        try:
            column_type = session.scalar(
                text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid='document_chunks'::regclass AND attname='embedding'"
                )
            )
            if column_type != "vector(1024)":
                raise RuntimeError(f"数据库向量字段不匹配：{column_type}。")
            record["stages"][stage] = {"status": "ok", "embedding_column": column_type}

            stage = "ingestion"
            teacher = User(
                username=f"rag-smoke-{uuid4().hex}",
                email=f"{uuid4().hex}@example.com",
                password_hash=hash_password(uuid4().hex),
            )
            role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
            teacher.roles.append(
                role or Role(name=UserRole.TEACHER, description="教师")
            )
            session.add(teacher)
            session.commit()
            course_summary = CourseService(session).create_course(
                name="检索增强生成冒烟课程", created_by=teacher.id
            )
            course = session.get(Course, UUID(course_summary.id))
            if course is None:
                raise RuntimeError("课程创建后无法读取持久化实体。")
            service = KnowledgeBaseService(session)
            knowledge_base = service.create_knowledge_base(
                course.id, "课程知识库", teacher_id=teacher.id
            )
            document = service.upload_document(
                course_id=course.id,
                knowledge_base_id=knowledge_base.id,
                uploaded_by=teacher.id,
                original_filename=lesson.name,
                storage_path=str(lesson),
            )
            provider = create_embedding_provider(settings)
            result = service.ingest_document(
                document.id,
                teacher_id=teacher.id,
                ingestion_service_factory=lambda listener: IngestionService(
                    embedding_provider=provider,
                    on_transition=listener,
                ),
            )
            record["embedding_provider"] = provider.describe()
            record["stages"][stage] = result.model_dump(mode="json")
            if result.status != DocumentStatus.READY:
                record["stages"][stage]["error_code"] = result.error_code
                raise RuntimeError(
                    result.detail or result.error_message or "摄取未进入 Ready。"
                )
            chunks = list(
                session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == document.id
                    )
                )
            )
            vector_count = sum(chunk.embedding is not None for chunk in chunks)
            search_count = sum(bool(chunk.search_vector) for chunk in chunks)
            dimensions = session.scalars(
                select(func.vector_dims(DocumentChunk.embedding)).where(
                    DocumentChunk.document_id == document.id
                )
            ).all()
            if (
                not chunks
                or vector_count != len(chunks)
                or search_count != len(chunks)
                or set(dimensions) != {1024}
            ):
                raise RuntimeError("片段、向量和全文字段数量或向量维度校验失败。")
            record["stages"][stage].update(
                chunk_count=len(chunks),
                vector_count=vector_count,
                search_vector_count=search_count,
                dimensions=list(dimensions),
            )

            stage = "hybrid"
            query = "向量检索如何用余弦相似度衡量课程资料的语义接近程度？"
            query_vector = asyncio.run(provider.embed_query(query))
            candidates = HybridSearchRetriever().search(
                session,
                RetrievalQuery(query, tuple(query_vector)),
                top_k=5,
                filters=RetrievalFilters(
                    course_ids=(course.id,), document_ids=(UUID(document.id),)
                ),
            )
            if not candidates or not all(
                item.document_id == document.id
                and item.course_id == str(course.id)
                and isinstance(item.metadata.get("chunk_index"), int)
                for item in candidates
            ):
                raise RuntimeError("Hybrid 未返回可追溯到本课程文档的片段。")
            record["query"] = query
            record["query_dimension"] = len(query_vector)
            record["stages"][stage] = {
                "status": "ok",
                "results": [item.as_dict() for item in candidates],
            }

            stage = "rerank"
            reranker = build_reranker()
            record["rerank_provider"] = reranker.describe()
            reranked = asyncio.run(
                reranker.rerank_async(query, candidates, top_k=len(candidates))
            )
            if {item.chunk_id for item in reranked} != {
                item.chunk_id for item in candidates
            } or not all(
                item.rerank_score is not None and 0 <= item.rerank_score <= 1
                for item in reranked
            ):
                raise RuntimeError("重排候选或分数未满足契约。")
            record["stages"][stage] = {
                "status": "ok",
                "results": [item.as_dict() for item in reranked],
            }
            record["status"] = "ok"
        except Exception as error:  # noqa: BLE001  # 运行失败必须写入记录，禁止伪造成功。
            failure = record["stages"].setdefault(stage, {})
            failure.update(status="failed")
            failure["error_code"] = failure.get("error_code") or getattr(
                error, "error_code", type(error).__name__
            )
            failure["error_message"] = (
                str(error) if isinstance(error, RuntimeError) else type(error).__name__
            )
            record.update(status="failed", failed_stage=stage)
        finally:
            try:
                session.rollback()
                if course is not None:
                    session.delete(course)
                if teacher is not None:
                    session.delete(teacher)
                session.commit()
                record["cleanup"] = "ok"
            except SQLAlchemyError as error:
                record.update(status="failed", cleanup=type(error).__name__)
            finally:
                record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                engine.dispose()
    print(
        json.dumps(
            {
                "status": record["status"],
                "failed_stage": record.get("failed_stage"),
                "record": str(output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if record["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
