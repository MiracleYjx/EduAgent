"""Benchmark 自有 schema、真实摄取与可移植标注映射；不改变生产摄取逻辑。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.ingestion.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS
from backend.app.ai.ingestion.service import IngestionService
from backend.app.core.config import AppSettings, get_settings
from backend.app.core.database import Base, create_database_engine
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import DocumentChunk, Role, User
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService
from backend.app.services.knowledge_base_service import KnowledgeBaseService

CORPUS_DIR = Path(__file__).resolve().parents[1] / "benchmark" / "corpus"
SCHEMA_PATTERN = re.compile(r"benchmark_[0-9a-f]{32}")


def fingerprint(value: Any) -> str:
    """稳定 JSON 摘要；仅用于比较输入/配置，不证明模型质量。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def embedding_metadata(provider: BaseEmbeddingProvider) -> dict[str, Any]:
    """只记录实际实例身份与影响编码的白名单参数，不序列化配置/凭据。"""
    metadata: dict[str, Any] = dict(provider.describe())
    metadata["call_path"] = (
        f"{type(provider).__module__}.{type(provider).__qualname__}"
    )
    for attribute in ("_query_prefix", "_batch_size"):
        value = getattr(provider, attribute, None)
        if value is not None:
            metadata[attribute.removeprefix("_")] = value
    # 同名云模型可能由不同端点提供；仅保留不透明摘要，不写 URL 或密钥。
    endpoint = getattr(getattr(provider, "_client", None), "base_url", None)
    if endpoint is not None:
        metadata["endpoint_fingerprint"] = fingerprint(str(endpoint))
    return metadata


def schema_engine(schema: str, settings: AppSettings | None = None) -> Engine:
    """连接且仅允许受控 schema 名称；使用前还必须验证 schema 存在。"""
    if SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError("不是 Benchmark 创建的隔离 schema。")
    engine = create_database_engine(settings or get_settings(), connect_timeout=5)

    @event.listens_for(engine, "connect")
    def set_search_path(connection, _record) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", public')
        connection.commit()

    return engine


def require_schema(engine: Engine, schema: str) -> None:
    """缺失隔离 schema 时拒绝回退读取 public 中的同名表。"""
    with engine.connect() as connection:
        if connection.scalar(text("SELECT current_schema()")) != schema:
            raise ValueError("manifest 对应 schema 不存在，请重新初始化；禁止回退 public。")


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 1:
        raise ValueError("不支持的 Benchmark manifest 版本。")
    if SCHEMA_PATTERN.fullmatch(str(manifest.get("schema", ""))) is None:
        raise ValueError("manifest 不是受控隔离 schema。")
    for chunk in manifest["corpus"]["chunks"]:
        if chunk["stable_id"] != f"{chunk['chunk_index']}:{fingerprint(chunk['content'])}":
            raise ValueError("manifest 片段内容与稳定标识不一致，请重新初始化。")
    inputs = _input_snapshot(
        manifest["inputs"]["source_sha256"], manifest["corpus"]["chunks"],
        manifest["queries"], manifest["annotations"], manifest["inputs"]["chunking"],
    )
    if inputs != manifest["inputs"] or fingerprint(inputs) != manifest["input_fingerprint"]:
        raise ValueError("manifest 查询/标注与输入摘要不一致，请重新初始化。")
    return manifest


def _input_snapshot(
    source_digest: str, chunks: list[dict[str, Any]], queries: list[dict[str, Any]],
    annotations: list[dict[str, Any]], chunking: dict[str, Any],
) -> dict[str, Any]:
    stable_ids = {item["chunk_id"]: item["stable_id"] for item in chunks}
    return {
        "source_sha256": source_digest,
        "chunking": chunking,
        "chunks": list(stable_ids.values()), "queries": queries,
        "labels": [{
            "query_id": item["query_id"], "out_of_scope": item.get("out_of_scope", False),
            "positives": sorted(stable_ids[key] for key in item["positive_chunk_ids"]),
        } for item in annotations],
    }


def cleanup_manifest(path: Path) -> None:
    """显式删除 manifest 所属 Benchmark schema，不删除报告和业务 schema。"""
    manifest = load_manifest(path)
    engine = schema_engine(manifest["schema"])
    try:
        require_schema(engine, manifest["schema"])
        with engine.begin() as connection:
            connection.execute(DropSchema(manifest["schema"], cascade=True))
    finally:
        engine.dispose()


def _map_annotations(
    reference: dict[str, Any], rows: list[DocumentChunk], annotations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """旧 UUID 只作为标注文件内键；精确内容和序号验证后替换成数据库真实 UUID。"""
    old_chunks = sorted(reference["chunks"], key=lambda item: item["chunk_index"])
    if not rows or len(rows) != len(old_chunks):
        raise ValueError("摄取片段数与标注基准不一致，请重新标注。")
    mapping: dict[str, str] = {}
    chunks = []
    for row, old in zip(rows, old_chunks, strict=True):
        if row.chunk_index != old["chunk_index"] or row.content != old["content"]:
            raise ValueError("摄取片段内容与标注基准不一致，请重新标注。")
        mapping[str(old["chunk_id"])] = str(row.id)
        chunks.append({
            "chunk_id": str(row.id), "chunk_index": row.chunk_index,
            "content": row.content,
            "stable_id": f"{row.chunk_index}:{fingerprint(row.content)}",
            "metadata": row.chunk_metadata,
        })
    remapped = []
    for annotation in annotations:
        item = dict(annotation)
        try:
            item["positive_chunk_ids"] = [
                mapping[str(old_id)] for old_id in item["positive_chunk_ids"]
            ]
            if "selection_methods" in item:
                item["selection_methods"] = {
                    mapping[str(old_id)]: methods
                    for old_id, methods in item["selection_methods"].items()
                }
        except KeyError as exc:
            raise ValueError("标注引用了未知片段，请重新标注。") from exc
        remapped.append(item)
    return chunks, remapped


def setup_corpus(
    output_dir: Path, *, provider: BaseEmbeddingProvider, corpus_dir: Path = CORPUS_DIR,
) -> Path:
    """读取教材，经生产服务摄取到全新 schema，输出独立 manifest；失败清理自有 schema。"""
    # 新目录防止覆写以往报告，也避免两个初始化复用同一 manifest。
    output_dir.mkdir(parents=True, exist_ok=False)
    source = corpus_dir / "python_basics.md"
    data = source.read_bytes()
    reference = json.loads((corpus_dir / "chunks.json").read_text(encoding="utf-8"))
    queries = json.loads((corpus_dir / "queries.json").read_text(encoding="utf-8"))
    annotations = json.loads((corpus_dir / "annotations.json").read_text(encoding="utf-8"))
    schema = f"benchmark_{uuid4().hex}"
    engine = schema_engine(schema)
    created = False
    try:
        with engine.begin() as connection:
            if not connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname='vector'")):
                raise ValueError("缺少 pgvector；先按开发文档运行 alembic upgrade head。")
            connection.execute(CreateSchema(schema))
        created = True
        require_schema(engine, schema)
        with engine.begin() as connection:
            Base.metadata.create_all(connection, checkfirst=False)
        with Session(engine, expire_on_commit=False) as session:
            teacher = User(
                username=f"benchmark-{uuid4().hex}", email=f"{uuid4().hex}@example.com",
                password_hash=hash_password(uuid4().hex),
                roles=[Role(name=UserRole.TEACHER, description="Benchmark 隔离教师")],
            )
            session.add(teacher)
            session.commit()
            course = CourseService(session).create_course(
                name="Benchmark 教材", created_by=teacher.id,
            )
            service = KnowledgeBaseService(session)
            knowledge_base = service.create_knowledge_base(
                course.id, "Benchmark 语料", teacher_id=teacher.id,
            )
            document = service.upload_document(
                course_id=course.id, knowledge_base_id=knowledge_base.id,
                uploaded_by=teacher.id, original_filename=source.name,
            )
            result = service.ingest_document(
                document.id, content=data, teacher_id=teacher.id,
                ingestion_service_factory=lambda listener: IngestionService(
                    embedding_provider=provider, on_transition=listener,
                ),
            )
            if result.status != DocumentStatus.READY:
                raise ValueError(f"摄取失败：{result.error_code}；{result.error_message}")
            rows = list(session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == document.id)
                .order_by(DocumentChunk.chunk_index)
            ))
            chunks, remapped = _map_annotations(reference, rows, annotations)
            inputs = _input_snapshot(
                hashlib.sha256(data).hexdigest(), chunks, queries, remapped,
                {"max_chars": DEFAULT_MAX_CHARS, "overlap_chars": DEFAULT_OVERLAP_CHARS},
            )
            manifest = {
                "manifest_version": 1, "created_at": datetime.now(UTC).isoformat(),
                "schema": schema, "source": source.name, "inputs": inputs,
                "input_fingerprint": fingerprint(inputs),
                "embedding": embedding_metadata(provider),
                "metadata": {"dataset_version": reference["dataset_version"]},
                "corpus": {"document_id": document.id, "chunk_count": len(chunks), "chunks": chunks},
                "queries": queries, "annotations": remapped,
            }
            path = output_dir / "manifest.json"
            with path.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
            return path
    except BaseException:
        if created:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        raise
    finally:
        engine.dispose()
