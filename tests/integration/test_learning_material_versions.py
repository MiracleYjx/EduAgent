"""T026 material versions and retrieval scope against real PostgreSQL/pgvector.

The real boundary here: a subject-owned material parses into anchored chunks,
only an explicit identity-checked index makes it retrievable, and version/series
history is never rewritten by a default-version switch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.ai.retrieval.base import (
    RetrievalFilters,
    RetrievalInputError,
    RetrievalQuery,
)
from backend.app.ai.retrieval.keyword_search import KeywordSearchRetriever
from backend.app.ai.retrieval.vector_search import VectorSearchRetriever
from backend.app.api.subjects import get_subject_service
from backend.app.core.bootstrap import BootstrapContext, create_bootstrap_app
from backend.app.models.question import Question
from backend.app.models.question_origin import QuestionOrigin
from backend.app.schemas.settings import (
    BootstrapOptions,
    BootstrapSettings,
    RuntimeKind,
)
from backend.app.services.learning_material_service import (
    LearningMaterialService,
    MaterialError,
)
from backend.app.services.settings.index_identity import IndexIdentityError
from backend.app.services.subject_service import SubjectService
from tests.contract.test_learning_materials import sample_docx
from tests.integration.test_personal_learning_foundation import migrate
from tests.postgres_helpers import (
    isolated_postgres_schema,
    isolated_test_resource_profile,
)

IDENTITY = {
    "provider": "openai_compatible",
    "adapter": "openai_compatible",
    "endpoint_identity": "http://127.0.0.1:9/v1",
    "model_id": "synthetic-test-embedding",
    "model_version": "1",
    "dimension": 1024,
    "semantic_parameters": {},
}
VECTOR = [1.0] + [0.0] * 1023
MARKDOWN = (
    "# 函数\n\n本章讨论函数的基本概念。\n\n"
    "## 定义\n\n给定定义域内的每个输入，都有唯一输出。\n\n"
    "## 性质\n\n单调性描述函数值随自变量的变化趋势。\n"
)


@pytest.fixture
def space(tmp_path):
    with (
        isolated_test_resource_profile((tmp_path / "space").resolve()) as profile,
        isolated_postgres_schema() as (engine, _schema),
    ):
        migrate(engine, "head")
        # 受管文件根必须先存在：缺失根是真实配置问题，不能靠测试隐式创建。
        profile.storage_root.mkdir(parents=True, exist_ok=True)
        subjects = SubjectService(
            sessionmaker(engine, expire_on_commit=False),
            storage_root=profile.storage_root,
        )
        subjects.profile = profile
        app = create_bootstrap_app(
            context=BootstrapContext(
                settings=BootstrapSettings(
                    environment=profile.environment,
                    environment_id=profile.environment_id,
                    runtime_kind=RuntimeKind.PYTHON,
                    config_root=profile.config_root,
                ),
                bind=BootstrapOptions(port=8000),
                static_root=None,
            )
        )
        app.dependency_overrides[get_subject_service] = lambda: subjects
        materials = LearningMaterialService(subjects)
        with TestClient(
            app,
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as client:
            yield SimpleNamespace(
                engine=engine,
                profile=profile,
                subjects=subjects,
                materials=materials,
                client=client,
            )


def subject(space, name: str = "数学") -> str:
    response = space.client.post("/api/learning/subjects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(space, subject_id: str, name: str, data: bytes, **payload):
    body = {"purpose": payload.pop("purpose", "study_material"), **payload}
    return space.client.post(
        f"/api/learning/subjects/{subject_id}/uploads",
        files={"file": (name, data, "application/octet-stream")},
        data={"metadata": json.dumps(body)},
    )


def index(space, material_id: str, *, identity=IDENTITY, vectors=None):
    view = space.materials.get(UUID(material_id))
    return space.materials.index_material(
        UUID(material_id),
        identity=identity,
        vectors=vectors if vectors is not None else [VECTOR] * view.chunk_count,
    )


def test_study_material_is_subject_owned_anchored_and_not_indexed_yet(space):
    sid = subject(space)
    response = upload(space, sid, "chapter.md", MARKDOWN.encode(), publisher="某出版社")
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["purpose"] == "study_material"
    assert value["state"] == "parsed"
    assert value["parse_state"] == "parsed"
    assert value["chunk_count"] > 0
    assert value["optional_metadata"] == {"publisher": "某出版社"}
    assert next(b["type"] for b in value["blocks"]) == "text"
    assert all(b["anchor"] for b in value["blocks"])
    with Session(space.engine) as session, session.begin():
        rows = session.execute(
            text(
                "SELECT subject_id, course_id, knowledge_base_id, chapter_id,"
                " embedding IS NULL, search_vector IS NOT NULL, metadata"
                " FROM document_chunks WHERE document_id=:id"
            ),
            {"id": value["id"]},
        ).all()
        assert rows
        for row in rows:
            assert str(row[0]) == sid and row[1] is None and row[2] is not None
            assert row[3] is None and row[4] is True and row[5] is True
            assert row[6]["subject_id"] == sid and row[6]["location"]
            assert row[6]["purpose"] == "knowledge_base"
        document = session.execute(
            text("SELECT status, material_state FROM documents WHERE id=:id"),
            {"id": value["id"]},
        ).one()
        assert document[0] == "Chunking" and document[1] == "parsed"


def test_paper_upload_keeps_source_but_never_becomes_teaching_knowledge(space):
    sid = subject(space)
    response = upload(
        space, sid, "试卷.txt", "1. 选择题 已知函数 f(x)=x+1".encode(), purpose="exam_paper"
    )
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["purpose"] == "exam_paper" and value["chunk_count"] == 0
    with Session(space.engine) as session:
        row = session.execute(
            text(
                "SELECT purpose, knowledge_base_id, subject_id FROM documents"
                " WHERE id=:id"
            ),
            {"id": value["id"]},
        ).one()
        assert str(row[0]) == "paper_source" and row[1] is None and str(row[2]) == sid
        assert session.execute(
            text("SELECT count(*) FROM document_chunks WHERE document_id=:id"),
            {"id": value["id"]},
        ).scalar_one() == 0


def test_docx_material_keeps_order_and_managed_media(space):
    sid = subject(space)
    response = upload(space, sid, "教材.docx", sample_docx())
    assert response.status_code == 201, response.text
    value = response.json()
    assert [b["type"] for b in value["blocks"]][:3] == ["text", "image", "text"]
    assert value["assets"] and value["assets"][0]["stored"] is True
    assert value["assets"][0]["media_type"] == "image/png"
    digest = value["assets"][0]["sha256"]

    stored = list((space.profile.storage_root / "materials").rglob(f"{digest}.png"))
    assert stored and stored[0].read_bytes()[:4] == b"\x89PNG"


def test_stored_material_asset_is_served_without_a_public_url(space):
    """原图必须能在不暴露可猜测 URL 的前提下按科目、资料和 digest 取回。"""

    sid = subject(space)
    value = upload(space, sid, "教材.docx", sample_docx()).json()
    digest = value["assets"][0]["sha256"]

    response = space.client.get(
        f"/api/learning/subjects/{sid}/materials/{value['id']}/assets/{digest}"
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.content[:4] == b"\x89PNG"


def test_material_asset_is_scoped_to_subject_material_and_digest(space):
    """跳科目、未知资料和未登记 digest 一律 404，不泄露存在性。"""

    sid = subject(space)
    other = subject(space, "物理")
    value = upload(space, sid, "教材.docx", sample_docx()).json()
    digest = value["assets"][0]["sha256"]

    for subject_id, material_id, candidate in (
        (other, value["id"], digest),
        (sid, value["id"], "0" * 64),
        (sid, str(uuid4()), digest),
    ):
        response = space.client.get(
            f"/api/learning/subjects/{subject_id}/materials/{material_id}"
            f"/assets/{candidate}"
        )
        assert response.status_code == 404, (subject_id, material_id, candidate)
        assert response.json()["code"] in {
            "MATERIAL_NOT_FOUND",
            "MATERIAL_ASSET_NOT_FOUND",
        }


def test_corrupted_upload_is_retained_with_real_error_and_oversize_is_rejected(space):
    sid = subject(space)
    broken = upload(space, sid, "broken.pdf", b"not a pdf")
    assert broken.status_code == 422
    assert broken.json()["code"] in {"DOCUMENT_CORRUPTED", "DOCUMENT_PARSE_FAILED"}
    listed = space.client.get(f"/api/learning/subjects/{sid}/materials").json()
    failed = [m for m in listed["materials"] if m["original_filename"] == "broken.pdf"]
    assert failed and failed[0]["state"] == "failed" and failed[0]["error_code"]

    oversized = upload(space, sid, "big.txt", b"a" * (20 * 1024 * 1024 + 1))
    assert oversized.status_code == 413
    assert oversized.json()["code"] in {"DOCUMENT_SIZE_LIMIT", "LIMIT_EXCEEDED"}
    after = space.client.get(f"/api/learning/subjects/{sid}/materials").json()
    assert len(after["materials"]) == len(listed["materials"])


def test_large_material_upload_is_transported_without_json_body_cap(space):
    """The dedicated multipart route carries real files; JSON writes stay capped."""

    sid = subject(space)
    payload = ("大数据资料正文。" * 12_000).encode()
    assert len(payload) > 64 * 1024
    response = upload(space, sid, "large.txt", payload)
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["chunk_count"] > 0 and value["state"] == "parsed"
    with Session(space.engine) as session:
        stored = session.execute(
            text("SELECT file_metadata FROM documents WHERE id=:id"),
            {"id": value["id"]},
        ).scalar_one()
        assert stored["size_bytes"] == len(payload)
    # 普通个人写接口仍受 64 KiB JSON 边界约束。
    capped = space.client.post(
        "/api/learning/subjects",
        content=b"{\"name\": \"" + b"x" * 70_000 + b"\"}",
        headers={"Content-Type": "application/json"},
    )
    assert capped.status_code == 413
    assert capped.json()["code"] == "LIMIT_EXCEEDED"


def test_optional_metadata_is_absent_rather_than_guessed(space):
    sid = subject(space)
    value = upload(space, sid, "plain.txt", "无描述资料正文。".encode()).json()
    assert value["optional_metadata"] == {}
    assert value["parse_state"] == "parsed"


def test_knowledge_suggestions_are_pending_until_user_confirmation(space):
    sid = subject(space)
    material = upload(space, sid, "chapter.md", MARKDOWN.encode()).json()
    suggested = space.client.post(
        f"/api/learning/subjects/{sid}/knowledge/suggest", json={}
    )
    assert suggested.status_code == 200, suggested.text
    suggested = suggested.json()
    assert suggested["confirmed"] is False
    assert all(c["confirmation_state"] == "suggested" for c in suggested["chapters"])
    assert any(p["confirmation_state"] == "pending" for p in suggested["knowledge_points"])
    titles = {c["title"] for c in suggested["chapters"]}
    assert "函数 / 定义" in titles

    target = next(c for c in suggested["chapters"] if c["title"] == "函数 / 定义")
    point = next(p for p in suggested["knowledge_points"] if p["name"] == "定义")
    confirmed = space.client.post(
        f"/api/learning/subjects/{sid}/knowledge/confirm",
        json={
            "knowledge_points": [{"id": point["id"], "name": "函数定义"}],
            "chapters": [
                {
                    "id": target["id"],
                    "title": "函数定义（已确认）",
                    "knowledge_point_ids": [point["id"]],
                    "document_id": material["id"],
                    "anchors": target["anchors"],
                }
            ],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    value = confirmed.json()
    chapter = next(c for c in value["chapters"] if c["id"] == target["id"])
    assert chapter["title"] == "函数定义（已确认）"
    assert chapter["confirmation_state"] == "confirmed"
    assert chapter["knowledge_point_ids"] == [point["id"]]
    with Session(space.engine) as session:
        metadata = session.execute(
            text(
                "SELECT metadata FROM document_chunks WHERE document_id=:id"
                " AND metadata->>'location' = :location"
            ),
            {"id": material["id"], "location": target["anchors"][0]},
        ).scalars().all()
        assert metadata and metadata[0]["knowledge_points"] == [point["id"]]
        assert metadata[0]["scope_confirmation"]["knowledge_points"] == {
            point["id"]: "confirmed"
        }
        assert metadata[0]["chapter_id"] == target["id"]


def test_cross_subject_and_cyclic_knowledge_are_rejected(space):
    first = subject(space)
    second = subject(space, "物理")
    other = space.client.post(
        f"/api/learning/subjects/{second}/knowledge/confirm",
        json={"knowledge_points": [{"name": "外科目知识点"}], "chapters": []},
    ).json()
    other_point = other["knowledge_points"][0]["id"]
    rejected = space.client.post(
        f"/api/learning/subjects/{first}/knowledge/confirm",
        json={
            "knowledge_points": [],
            "chapters": [
                {"title": "越界章节", "knowledge_point_ids": [other_point]}
            ],
        },
    )
    assert rejected.status_code == 404

    parent = space.client.post(
        f"/api/learning/subjects/{first}/knowledge/confirm",
        json={"knowledge_points": [], "chapters": [{"title": "父章节"}]},
    ).json()["chapters"][0]["id"]
    child = space.client.post(
        f"/api/learning/subjects/{first}/knowledge/confirm",
        json={
            "knowledge_points": [],
            "chapters": [{"title": "子章节", "parent_id": parent}],
        },
    ).json()
    child_id = next(c["id"] for c in child["chapters"] if c["title"] == "子章节")
    cyclic = space.client.post(
        f"/api/learning/subjects/{first}/knowledge/confirm",
        json={
            "knowledge_points": [],
            "chapters": [{"id": parent, "title": "父章节", "parent_id": child_id}],
        },
    )
    assert cyclic.status_code == 409
    assert cyclic.json()["code"] == "KNOWLEDGE_HIERARCHY_CYCLE"


def test_retrieval_requires_index_and_respects_subject_purpose_and_identity(space):
    sid = subject(space)
    material = upload(space, sid, "chapter.md", MARKDOWN.encode()).json()
    keyword = KeywordSearchRetriever()
    with space.subjects.sessions() as session:
        unindexed = keyword.search(
            session, "函数", filters=RetrievalFilters(subject_ids=(UUID(sid),))
        )
    assert unindexed == []

    view = index(space, material["id"])
    assert view.state == "indexed" and view.parse_state in {"parsed", "partial"}
    with space.subjects.sessions() as session:
        hits = keyword.search(
            session,
            "函数",
            filters={"subject_ids": (sid,)},
        )
        assert hits and all(hit.subject_id == sid for hit in hits)
        assert all(hit.course_id == "" for hit in hits)
        vector_hits = VectorSearchRetriever(index_identity=IDENTITY).search(
            session, VECTOR, filters={"subject_ids": (sid,)}
        )
        assert vector_hits and vector_hits[0].document_id == material["id"]
        # 试卷用途从不进入教学检索。
        upload(space, sid, "试卷.txt", "1. 选择题 函数".encode(), purpose="exam_paper")
        assert all(
            hit.document_id != material["id"] or hit.subject_id == sid
            for hit in vector_hits
        )

    other = subject(space, "物理")
    with space.subjects.sessions() as session:
        assert keyword.search(session, "函数", filters={"subject_ids": (other,)}) == []


def test_embedding_identity_and_dimension_mismatch_fail_explicitly(space):
    sid = subject(space)
    first = upload(space, sid, "chapter.md", MARKDOWN.encode()).json()
    second = upload(space, sid, "chapter2.md", MARKDOWN.encode()).json()
    index(space, first["id"])
    with pytest.raises(MaterialError) as wrong_model:
        index(
            space,
            second["id"],
            identity={**IDENTITY, "model_id": "another-embedding"},
        )
    assert wrong_model.value.code == "EMBEDDING_INCOMPATIBLE"
    with pytest.raises(MaterialError) as wrong_dimension:
        index(space, second["id"], identity={**IDENTITY, "dimension": 512})
    assert wrong_dimension.value.code == "EMBEDDING_DIMENSION_MISMATCH"
    with space.subjects.sessions() as session:
        with pytest.raises(IndexIdentityError):
            VectorSearchRetriever(
                index_identity={**IDENTITY, "model_id": "another-embedding"}
            ).search(session, VECTOR, filters={"subject_ids": (sid,)})
        with pytest.raises(RetrievalInputError):
            VectorSearchRetriever(index_identity=IDENTITY).search(
                session, [1.0, 0.0, 0.0], filters={"subject_ids": (sid,)}
            )


def test_version_default_switch_changes_only_new_requests(space):
    sid = subject(space)
    first = upload(space, sid, "v1.md", MARKDOWN.encode()).json()
    second = upload(
        space, sid, "v2.md", (MARKDOWN + "\n## 新增\n\n新版补充内容。\n").encode()
    ).json()
    series = space.client.post(
        f"/api/learning/subjects/{sid}/material-series",
        json={"document_id": first["id"], "series_name": "数学教材", "label": "第一版"},
    )
    assert series.status_code == 201, series.text
    series_id = series.json()["id"]
    assert series.json()["default_version_id"] is not None
    v2 = space.client.post(
        f"/api/learning/subjects/{sid}/material-series",
        json={"document_id": second["id"], "series_id": series_id, "label": "第二版"},
    )
    assert v2.status_code == 201, v2.text
    assert v2.json()["default_version_id"] == series.json()["default_version_id"]
    v1_version = series.json()["default_version_id"]
    v2_version = next(v["id"] for v in v2.json()["versions"] if v["label"] == "第二版")

    index(space, first["id"])
    index(space, second["id"])
    with Session(space.engine) as session, session.begin():
        session.add(Question(subject_id=UUID(sid), status="Draft"))
        session.flush()
        question_id = session.execute(
            text("SELECT id FROM questions WHERE subject_id=:sid"), {"sid": sid}
        ).scalar_one()
        session.add(
            QuestionOrigin(
                question_id=question_id,
                subject_id=UUID(sid),
                source_kind="uploaded",
                document_id=UUID(first["id"]),
                source_location={"location": "第 1 段"},
                answer_origin="paper",
            )
        )

    switched = space.client.post(
        f"/api/learning/material-series/{series_id}/default-version",
        json={"version_id": v2_version},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["default_version_id"] == v2_version
    with space.subjects.sessions() as session:
        versioned = VectorSearchRetriever(index_identity=IDENTITY).search(
            session, VECTOR, filters={"material_version_ids": (v1_version,)}
        )
        assert {hit.document_id for hit in versioned} == {first["id"]}
        default_hits = VectorSearchRetriever(index_identity=IDENTITY).search(
            session, VECTOR, filters={"material_version_ids": (v2_version,)}
        )
        assert {hit.document_id for hit in default_hits} == {second["id"]}

    with Session(space.engine) as session:
        origin = session.execute(
            text("SELECT document_id, source_location FROM question_origins")
        ).one()
        assert str(origin[0]) == first["id"]
        assert origin[1] == {"location": "第 1 段"}
        # 默认切换只改系列指针：版本行与旧资料本身都不会被改写。
        version_row = session.execute(
            text("SELECT document_id, label FROM material_versions WHERE id=:id"),
            {"id": v1_version},
        ).one()
        assert str(version_row[0]) == first["id"] and version_row[1] == "第一版"
        # 旧版本仍可打开，来源事实未被新默认版本覆盖。
        old = space.client.get(f"/api/learning/materials/{first['id']}").json()
        assert old["version_id"] == v1_version and old["state"] == "indexed"


def test_materials_are_not_readable_from_another_subject(space):
    sid = subject(space)
    other = subject(space, "物理")
    material = upload(space, sid, "chapter.md", MARKDOWN.encode()).json()
    direct = space.client.get(f"/api/learning/materials/{material['id']}")
    assert direct.status_code == 200
    listed = space.client.get(f"/api/learning/subjects/{other}/materials").json()
    assert listed["materials"] == []
    unknown = space.client.get(f"/api/learning/materials/{uuid4()}")
    assert unknown.status_code == 404

    with pytest.raises(DBAPIError), space.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document_chunks (id, document_id, course_id, subject_id,"
                " knowledge_base_id, chunk_index, content, metadata)"
                " SELECT :new, id, NULL, :other, knowledge_base_id, 999, '越界', '{}'"
                " FROM documents WHERE id=:doc"
            ),
            {"new": uuid4(), "other": other, "doc": material["id"]},
        )


def test_chapter_and_knowledge_point_scope_filters_real_chunks(space):
    sid = subject(space)
    material = upload(space, sid, "chapter.md", MARKDOWN.encode()).json()
    suggested = space.client.post(
        f"/api/learning/subjects/{sid}/knowledge/suggest", json={}
    )
    assert suggested.status_code == 200, suggested.text
    suggested = suggested.json()
    target = next(c for c in suggested["chapters"] if c["title"] == "函数 / 性质")
    point = next(p for p in suggested["knowledge_points"] if p["name"] == "性质")
    space.client.post(
        f"/api/learning/subjects/{sid}/knowledge/confirm",
        json={
            "knowledge_points": [{"id": point["id"], "name": point["name"]}],
            "chapters": [
                {
                    "id": target["id"],
                    "title": target["title"],
                    "knowledge_point_ids": [point["id"]],
                    "document_id": material["id"],
                    "anchors": target["anchors"],
                }
            ],
        },
    )
    index(space, material["id"])
    # PostgreSQL simple 分词不分中文子串，因此必须用真实存在的完整 token 查询。
    query_text = "单调性描述函数值随自变量的变化趋势"
    with space.subjects.sessions() as session:
        hits = KeywordSearchRetriever().search(
            session,
            query_text,
            filters={"subject_ids": (sid,)},
        )
        assert hits and all(str(hit.metadata.get("chapter_id")) == target["id"] for hit in hits)
        scoped = KeywordSearchRetriever().search(
            session,
            RetrievalQuery(
                text=query_text,
                embedding=(),
                chapter_ids=(UUID(target["id"]),),
            ),
            filters={"subject_ids": (sid,)},
        )
        assert scoped and {hit.chunk_id for hit in scoped} == {hit.chunk_id for hit in hits}
        by_point = KeywordSearchRetriever().search(
            session,
            RetrievalQuery(
                text=query_text, embedding=(), knowledge_points=(point["id"],)
            ),
            filters={"subject_ids": (sid,)},
        )
        assert by_point and all(hit.metadata["knowledge_points"] == [point["id"]] for hit in by_point)


def _scanned_pdf_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 300), "white")
    ImageDraw.Draw(image).text((20, 40), "SCAN FACT", fill="black")
    output = BytesIO()
    image.save(output, format="PDF")
    image.close()
    return output.getvalue()


def test_scanned_pdf_has_a_readable_managed_original_page(space):
    import hashlib

    sid = subject(space)
    response = upload(space, sid, "scan.pdf", _scanned_pdf_bytes())
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["requires_ocr"] is True
    assert value["parse_state"] == "partial" and value["chunk_count"] == 0
    block = next(block for block in value["blocks"] if block["type"] == "image")
    assert block["page"] == 1 and block["sha256"]
    assert value["assets"] and value["assets"][0]["stored"] is True
    original = space.client.get(
        f"/api/learning/subjects/{sid}/materials/{value['id']}/assets/{block['sha256']}"
    )
    assert original.status_code == 200, original.text
    assert original.content.startswith(b"\x89PNG")
    assert hashlib.sha256(original.content).hexdigest() == block["sha256"]


def test_pdf_page_batch_does_not_claim_unrendered_originals(space):
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(11):
        writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    sid = subject(space)
    response = upload(space, sid, "eleven.pdf", output.getvalue())
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["parse_state"] == "partial"
    assert {block["page"] for block in value["blocks"]} == set(range(1, 11))
    assert all(block["sha256"] for block in value["blocks"])
    with Session(space.engine) as session:
        facts = session.execute(
            text("SELECT parse_facts FROM documents WHERE id=:id"),
            {"id": value["id"]},
        ).scalar_one()
    assert facts["unprocessed_pages"] == [11]
    assert facts["page_count"] == 11


def test_pdf_decode_limit_is_enforced_and_failure_is_persisted(space):
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=1000, height=1000)
    output = BytesIO()
    writer.write(output)
    sid = subject(space)
    response = upload(space, sid, "over-budget.pdf", output.getvalue())
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "PAPER_PAGE_TOO_LARGE"
    value = space.client.get(
        f"/api/learning/subjects/{sid}/materials"
    ).json()["materials"][0]
    assert value["state"] == "failed"
    assert value["error_code"] == "PAPER_PAGE_TOO_LARGE"
    assert value["assets"] == []


def test_material_asset_digest_rejects_corrupted_stored_bytes(space):
    sid = subject(space)
    value = upload(space, sid, "asset.docx", sample_docx()).json()
    digest = value["assets"][0]["sha256"]
    stored = next(
        (space.profile.storage_root / "materials").rglob(f"{digest}.png")
    )
    stored.write_bytes(b"synthetic corrupted image")
    response = space.client.get(
        f"/api/learning/subjects/{sid}/materials/{value['id']}/assets/{digest}"
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "MATERIAL_ASSET_CORRUPTED"
    assert b"synthetic corrupted image" not in response.content
