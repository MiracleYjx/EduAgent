"""Benchmark 自有 schema、真实摄取与可移植标注映射；不改变生产摄取逻辑。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.ingestion.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS
from backend.app.ai.ingestion.service import IngestionResult, IngestionService
from backend.app.core.config import AppSettings, get_settings
from backend.app.core.database import Base, create_database_engine
from backend.app.core.environment import (
    authorize_cleanup,
    environment_path,
    load_test_resource_profile,
    resolve_environment_profile,
    resolve_results_directory,
    select_environment,
)
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import DocumentChunk, Role, User
from backend.app.schemas.settings import (
    DatabaseIdentity,
    EnvironmentName,
    OwnedResource,
    ResourceManifest,
    TestResourceProfile,
)
from backend.app.services.auth_service import hash_password
from backend.app.services.course_service import CourseService
from backend.app.services.knowledge_base_service import KnowledgeBaseService
from scripts.benchmark_runtime import BenchmarkValidationError, close_owned

# Current production Markdown boundaries; legacy corpus/results remain unchanged.
CORPUS_DIR = (
    Path(__file__).resolve().parents[1]
    / "benchmark" / "corpus" / "heading-v3-20261002"
)
SCHEMA_PATTERN = re.compile(r"benchmark_[0-9a-f]{32}")
TEST_SCHEMA_PATTERN = re.compile(r"test_([0-9a-f]{32})")


@dataclass
class _TestSchemaLease:
    profile: TestResourceProfile
    schema: str
    manifest_path: Path
    record: ResourceManifest | None = None
    persisted: bool = False


def _test_profile(settings: AppSettings | None = None) -> TestResourceProfile | None:
    if select_environment() != EnvironmentName.TEST:
        return None
    profile = load_test_resource_profile()
    url = make_url(str((settings or get_settings()).database_url))
    identity = profile.database_identity
    if (url.host, url.port or 5432, url.database, url.username) != (
        identity.host, identity.port, identity.database, identity.user,
    ):
        raise BenchmarkValidationError("TEST_DATABASE_URL_MISMATCH")
    resolve_environment_profile()  # Also reject an inherited, conflicting DATA_ROOT.
    return profile


def _validate_schema(schema: str, profile: TestResourceProfile | None) -> None:
    if profile is not None:
        if TEST_SCHEMA_PATTERN.fullmatch(schema) is None:
            raise BenchmarkValidationError("Test Benchmark 只接受自有 schema；禁止回退 public。")
    elif SCHEMA_PATTERN.fullmatch(schema) is None:
        raise BenchmarkValidationError("不是 Benchmark 创建的隔离 schema。")


def _test_receipt_root(profile: TestResourceProfile, schema: str) -> Path:
    _validate_schema(schema, profile)
    selected = resolve_environment_profile()
    if (selected.environment != EnvironmentName.TEST or selected.environment_id != profile.environment_id
            or selected.config_root.parent != profile.root):
        raise BenchmarkValidationError("TEST_BENCHMARK_PROFILE_MISMATCH")
    return environment_path(selected, "temp", f"benchmark/{schema[5:]}")


def _test_receipt_path(profile: TestResourceProfile, schema: str, filename: str) -> Path:
    path = _test_receipt_root(profile, schema) / filename
    if path != path.resolve():
        raise BenchmarkValidationError("TEST_BENCHMARK_RECEIPT_PATH_UNVERIFIED")
    return path


def _test_manifest_path(profile: TestResourceProfile, path: Path) -> Path:
    target = path.resolve()
    selected = resolve_environment_profile()
    if selected.environment != EnvironmentName.TEST or selected.environment_id != profile.environment_id:
        raise BenchmarkValidationError("TEST_BENCHMARK_PROFILE_MISMATCH")
    base = environment_path(selected, "results")
    if (not target.is_relative_to(base) or target == base
            or (path.is_absolute() and path != target)
            or base != profile.root / "results"):
        raise BenchmarkValidationError("TEST_BENCHMARK_MANIFEST_OUTSIDE_PROFILE")
    return target


def _validate_test_record(profile: TestResourceProfile, schema: str, record: ResourceManifest) -> None:
    identity = profile.database_identity.model_copy(update={"schema_name": schema})
    if (record.environment != EnvironmentName.TEST or record.environment_id != profile.environment_id
            or record.run_id != UUID(hex=schema[5:]) or record.database_identity != identity
            or record.root != _test_receipt_root(profile, schema)
            or record.cleanup_state != "not_started" or record.redis_instance_id is not None
            or len(record.resources) != 1):
        raise BenchmarkValidationError("TEST_BENCHMARK_RECEIPT_MISMATCH")
    resource = record.resources[0]
    if (resource.kind != "schema" or resource.target != schema or resource.run_id != record.run_id
            or resource.owner != identity.user):
        raise BenchmarkValidationError("TEST_BENCHMARK_RESOURCE_NOT_OWNED")


def _load_test_receipt(
    profile: TestResourceProfile, schema: str, *, manifest_path: Path | None = None,
) -> tuple[ResourceManifest, Path]:
    completed_path = _test_receipt_path(profile, schema, "cleanup.json")
    creation_path = _test_receipt_path(profile, schema, "creation.json")
    if completed_path.exists():
        raise BenchmarkValidationError("Test Benchmark schema 已清理；禁止回退 public。")
    try:
        payload = json.loads(creation_path.read_text(encoding="utf-8"))
        if set(payload) != {"receipt_version", "manifest_path", "resource_manifest"} or payload["receipt_version"] != 1:
            raise BenchmarkValidationError("TEST_BENCHMARK_RECEIPT_FORMAT")
        record = ResourceManifest.model_validate(payload["resource_manifest"])
        original_path = Path(payload["manifest_path"])
        if not original_path.is_absolute():
            raise BenchmarkValidationError("TEST_BENCHMARK_ABSOLUTE_MANIFEST_REQUIRED")
        original_path = _test_manifest_path(profile, original_path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise BenchmarkValidationError("缺少或无效的 Test Benchmark 创建收据；禁止回退 public。") from exc
    _validate_test_record(profile, schema, record)
    if manifest_path is not None and _test_manifest_path(profile, manifest_path) != original_path:
        raise BenchmarkValidationError("TEST_BENCHMARK_MANIFEST_NOT_OWNED_BY_RUN")
    return record, original_path


def _persist_test_receipt(lease: _TestSchemaLease) -> None:
    record = lease.record
    if record is None:
        raise BenchmarkValidationError("TEST_BENCHMARK_CREATION_RECEIPT_REQUIRED")
    _validate_test_record(lease.profile, lease.schema, record)
    root = _test_receipt_root(lease.profile, lease.schema)
    root.mkdir(parents=True, exist_ok=False)
    # Independent, exclusive creation evidence: never infer ownership from a
    # mutable corpus manifest, and never overwrite another run's receipt.
    with _test_receipt_path(lease.profile, lease.schema, "creation.json").open("x", encoding="utf-8") as handle:
        json.dump({"receipt_version": 1, "manifest_path": str(lease.manifest_path),
                   "resource_manifest": record.model_dump(mode="json")}, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    lease.persisted = True


def _verify_test_checkout(cursor: Any, lease: _TestSchemaLease) -> None:
    def scalar(statement: str, parameters: tuple[str, ...] = ()) -> Any:
        cursor.execute(statement, parameters)
        row = cursor.fetchone()
        return row[0] if row is not None else None

    record = lease.record
    if record is not None:
        _validate_test_record(lease.profile, lease.schema, record)
        if lease.persisted:
            saved, original_path = _load_test_receipt(lease.profile, lease.schema)
            if saved != record or original_path != lease.manifest_path:
                raise BenchmarkValidationError("TEST_BENCHMARK_RECEIPT_CHANGED")
    expected_schema = lease.schema if record is not None else "public"
    actual = DatabaseIdentity(
        host=lease.profile.database_identity.host,
        port=lease.profile.database_identity.port,
        database=str(scalar("SELECT current_database()")),
        user=str(scalar("SELECT current_user")),
        schema_name=str(scalar("SELECT current_schema()")),
        instance_id=str(scalar("SELECT system_identifier::text FROM pg_control_system()")),
    )
    expected = lease.profile.database_identity.model_copy(update={"schema_name": expected_schema})
    if actual != expected:
        raise BenchmarkValidationError("TEST_BENCHMARK_DATABASE_IDENTITY_MISMATCH；禁止回退 public。")
    owner = scalar("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s", (lease.schema,))
    if record is None:
        if owner is not None:
            raise BenchmarkValidationError("TEST_BENCHMARK_SCHEMA_ALREADY_EXISTS")
    else:
        if owner != record.resources[0].owner:
            raise BenchmarkValidationError("TEST_BENCHMARK_SCHEMA_OWNER_CHANGED")
        authorize_cleanup(record, record.resources[0], actual_database=actual, actual_redis_instance=None)


def _test_schema_engine(lease: _TestSchemaLease, settings: AppSettings | None = None) -> Engine:
    runtime = settings or get_settings()
    if _test_profile(runtime) != lease.profile:
        raise BenchmarkValidationError("TEST_BENCHMARK_PROFILE_MISMATCH")
    engine = create_database_engine(runtime, connect_timeout=5)
    # Expose the exact lease to controlled execution observers; it grants no
    # authority by itself. Every checkout still verifies server/owner/receipt.
    vars(engine)["eduagent_test_lease"] = lease

    @event.listens_for(engine, "checkout")
    def guard_checkout(connection, _record, _proxy) -> None:
        try:
            if _test_profile(runtime) != lease.profile:
                raise BenchmarkValidationError("TEST_BENCHMARK_PROFILE_MISMATCH")
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{lease.schema}", public')
                _verify_test_checkout(cursor, lease)
            connection.commit()
        except BaseException as exc:
            try:
                connection.rollback()
            except Exception as rollback_error:  # noqa: BLE001 - retain the original rejection
                exc.add_note(f"Test checkout rollback failed: {type(rollback_error).__name__}")
            raise

    return engine


def _drop_test_schema(engine: Engine, lease: _TestSchemaLease) -> None:
    if lease.record is None:
        raise BenchmarkValidationError("TEST_BENCHMARK_CREATION_RECEIPT_REQUIRED")
    # Checkout rechecks the actual instance, role, schema, owner and receipt
    # immediately before the destructive transaction; T199 remains authoritative.
    with engine.begin() as connection:
        connection.execute(DropSchema(lease.schema, cascade=True))
    if lease.persisted:
        completed = lease.record.model_copy(update={"cleanup_state": "completed"})
        with _test_receipt_path(lease.profile, lease.schema, "cleanup.json").open("x", encoding="utf-8") as handle:
            handle.write(completed.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())


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
    profile = _test_profile(settings)
    _validate_schema(schema, profile)
    if profile is not None:
        record, original_path = _load_test_receipt(profile, schema)
        return _test_schema_engine(_TestSchemaLease(profile, schema, original_path, record, True), settings)
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
            raise BenchmarkValidationError("manifest 对应 schema 不存在，请重新初始化；禁止回退 public。")


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 1:
        raise BenchmarkValidationError("不支持的 Benchmark manifest 版本。")
    profile = _test_profile()
    if profile is not None:
        _validate_schema(str(manifest.get("schema", "")), profile)
    elif SCHEMA_PATTERN.fullmatch(str(manifest.get("schema", ""))) is None:
        raise BenchmarkValidationError("manifest 不是受控隔离 schema。")
    for chunk in manifest["corpus"]["chunks"]:
        if chunk["stable_id"] != f"{chunk['chunk_index']}:{fingerprint(chunk['content'])}":
            raise BenchmarkValidationError("manifest 片段内容与稳定标识不一致，请重新初始化。")
    inputs = _input_snapshot(
        manifest["inputs"]["source_sha256"], manifest["corpus"]["chunks"],
        manifest["queries"], manifest["annotations"], manifest["inputs"]["chunking"],
    )
    if inputs != manifest["inputs"] or fingerprint(inputs) != manifest["input_fingerprint"]:
        raise BenchmarkValidationError("manifest 查询/标注与输入摘要不一致，请重新初始化。")
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
    profile = _test_profile()
    lease = None
    if profile is not None:
        record, original_path = _load_test_receipt(profile, manifest["schema"], manifest_path=path)
        lease = _TestSchemaLease(profile, manifest["schema"], original_path, record, True)
        engine = _test_schema_engine(lease)
    else:
        engine = schema_engine(manifest["schema"])
    try:
        require_schema(engine, manifest["schema"])
        if lease is not None:
            _drop_test_schema(engine, lease)
        else:
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
        raise BenchmarkValidationError("摄取片段数与标注基准不一致，请重新标注。")
    mapping: dict[str, str] = {}
    chunks = []
    for row, old in zip(rows, old_chunks, strict=True):
        if row.chunk_index != old["chunk_index"] or row.content != old["content"]:
            raise BenchmarkValidationError("摄取片段内容与标注基准不一致，请重新标注。")
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
            raise BenchmarkValidationError("标注引用了未知片段，请重新标注。") from exc
        remapped.append(item)
    return chunks, remapped


def setup_corpus(
    output_dir: Path, *, provider: BaseEmbeddingProvider, corpus_dir: Path = CORPUS_DIR,
    close_provider: bool = False,
) -> Path:
    """读取教材，经生产服务摄取到全新 schema，输出独立 manifest；失败清理自有 schema。"""
    closed = False

    async def close_once() -> None:
        nonlocal closed
        if close_provider and not closed:
            closed = True
            await close_owned(provider)

    class OwnedIngestion(IngestionService):
        async def ingest(self, **kwargs: Any) -> IngestionResult:
            try:
                return await super().ingest(**kwargs)
            finally:
                await close_once()

    with ExitStack() as clients:
        clients.callback(lambda: asyncio.run(close_once()))
        profile = _test_profile()
        if profile is not None:
            output_dir = resolve_results_directory(output_dir)
        # 新目录防止覆写以往报告，也避免两个初始化复用同一 manifest。
        output_dir.mkdir(parents=True, exist_ok=False)
        source = corpus_dir / "python_basics.md"
        data = source.read_bytes()
        reference = json.loads((corpus_dir / "chunks.json").read_text(encoding="utf-8"))
        queries = json.loads((corpus_dir / "queries.json").read_text(encoding="utf-8"))
        annotations = json.loads((corpus_dir / "annotations.json").read_text(encoding="utf-8"))
        run_id = uuid4()
        schema = f"test_{run_id.hex}" if profile is not None else f"benchmark_{run_id.hex}"
        lease = None
        if profile is not None:
            lease = _TestSchemaLease(profile, schema, _test_manifest_path(profile, output_dir / "manifest.json"))
            engine = _test_schema_engine(lease)
        else:
            engine = schema_engine(schema)
        created = False
        try:
            with engine.begin() as connection:
                if not connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname='vector'")):
                    raise BenchmarkValidationError("缺少 pgvector；先按开发文档运行 alembic upgrade head。")
                connection.execute(CreateSchema(schema))
                if lease is not None:
                    owner = connection.scalar(text(
                        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = :name"
                    ), {"name": schema})
                    if owner != lease.profile.database_identity.user:
                        raise BenchmarkValidationError("TEST_BENCHMARK_SCHEMA_OWNER_MISMATCH")
            created = True
            if lease is not None:
                resource = OwnedResource(kind="schema", target=schema, owner=str(owner),
                                         run_id=run_id, created_at=datetime.now(UTC))
                lease.record = ResourceManifest(
                    environment=lease.profile.environment, environment_id=lease.profile.environment_id, run_id=run_id,
                    database_identity=lease.profile.database_identity.model_copy(update={"schema_name": schema}),
                    root=_test_receipt_root(lease.profile, schema), resources=(resource,),
                )
                _persist_test_receipt(lease)
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
                    ingestion_service_factory=lambda listener: OwnedIngestion(
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
        except BaseException as exc:
            if created:
                if lease is not None:
                    try:
                        _drop_test_schema(engine, lease)
                    except Exception as cleanup_error:  # noqa: BLE001 - never bypass a failed ownership check
                        exc.add_note(f"Test schema {schema} cleanup failed: {type(cleanup_error).__name__}; resource retained.")
                else:
                    with engine.begin() as connection:
                        connection.execute(DropSchema(schema, cascade=True))
            raise
        finally:
            engine.dispose()
