"""Upload transport security: streaming bounds, chunked bodies and temp cleanup.

These tests target the dedicated ``multipart/form-data`` route and the fact that
the ordinary personal JSON boundary is unchanged. No model call and no network
access is involved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.app.api.subjects import get_subject_service
from backend.app.core.bootstrap import BootstrapContext, create_bootstrap_app
from backend.app.schemas.settings import (
    BootstrapOptions,
    BootstrapSettings,
    RuntimeKind,
)
from backend.app.services.learning_material_service import LearningMaterialService
from backend.app.services.subject_service import SubjectService
from tests.integration.test_personal_learning_foundation import migrate
from tests.postgres_helpers import (
    isolated_postgres_schema,
    isolated_test_resource_profile,
)

SMALL = "第 1 段 教学资料正文。".encode()
BOUNDARY = "----eduagent-test-boundary"


class ClientAbort(RuntimeError):
    """Abort the request body mid-stream to prove temp cleanup."""


def leaf_exceptions(error: BaseException) -> list[BaseException]:
    """Flatten the task-group wrapper so the real abort cause is asserted."""

    if isinstance(error, BaseExceptionGroup):
        return [leaf for item in error.exceptions for leaf in leaf_exceptions(item)]
    return [error]


@pytest.fixture
def space(tmp_path):
    with (
        isolated_test_resource_profile((tmp_path / "space").resolve()) as profile,
        isolated_postgres_schema() as (engine, _schema),
    ):
        migrate(engine, "head")
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
        with TestClient(
            app,
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as client:
            yield SimpleNamespace(
                engine=engine,
                profile=profile,
                subjects=subjects,
                materials=LearningMaterialService(subjects),
                client=client,
            )


def subject(space, name: str = "数学") -> str:
    response = space.client.post("/api/learning/subjects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def incoming(space):
    directory = space.profile.storage_root / "materials" / ".incoming"
    return sorted(directory.glob("*")) if directory.is_dir() else []


def stream_body(chunks, *, filename="notes.txt", metadata=None):
    """Yield multipart bytes so the client uses chunked transfer encoding."""

    meta = json.dumps(metadata or {"purpose": "study_material"}).encode()
    yield f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n".encode()
    yield b"Content-Type: application/json\r\n\r\n" + meta + b"\r\n"
    yield (
        f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode()
    yield from chunks
    yield f"\r\n--{BOUNDARY}--\r\n".encode()


def post_stream(space, subject_id: str, chunks, **kwargs):
    return space.client.post(
        f"/api/learning/subjects/{subject_id}/uploads",
        content=stream_body(chunks, **kwargs),
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
    )


def test_normal_multipart_upload_reports_real_bytes(space):
    sid = subject(space)
    response = space.client.post(
        f"/api/learning/subjects/{sid}/uploads",
        files={"file": ("notes.txt", SMALL, "text/plain")},
        data={"metadata": json.dumps({"purpose": "study_material", "year": 2024})},
    )
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["state"] == "parsed" and value["chunk_count"] >= 1
    assert value["optional_metadata"] == {"year": 2024}
    assert incoming(space) == []


def test_chunked_body_without_content_length_is_accepted(space):
    sid = subject(space)
    response = post_stream(space, sid, [SMALL[:5], SMALL[5:]])
    assert response.status_code == 201, response.text
    assert response.json()["chunk_count"] >= 1
    assert incoming(space) == []


def test_over_limit_stream_is_refused_and_temp_file_removed(space):
    sid = subject(space)
    limit = space.materials.limits.upload_bytes
    chunks = (b"x" * 262_144 for _ in range(limit // 262_144 + 2))
    response = post_stream(space, sid, chunks)
    assert response.status_code == 413
    assert response.json()["code"] == "LIMIT_EXCEEDED"
    assert incoming(space) == []
    listed = space.client.get(f"/api/learning/subjects/{sid}/materials").json()
    assert listed["materials"] == []


def test_interrupted_client_stream_leaves_no_temp_file(space):
    sid = subject(space)

    def broken():
        yield SMALL[:4]
        raise ClientAbort("client aborted mid-body")

    with pytest.raises(BaseExceptionGroup) as error:
        post_stream(space, sid, broken())
    assert any(isinstance(item, ClientAbort) for item in leaf_exceptions(error.value))
    assert incoming(space) == []


def test_missing_file_part_and_bad_metadata_are_rejected(space):
    sid = subject(space)
    no_file = space.client.post(
        f"/api/learning/subjects/{sid}/uploads",
        files={"not_a_file": ("x.txt", SMALL, "text/plain")},
        data={"metadata": json.dumps({"purpose": "study_material"})},
    )
    assert no_file.status_code == 422
    bad_meta = space.client.post(
        f"/api/learning/subjects/{sid}/uploads",
        files={"file": ("notes.txt", SMALL, "text/plain")},
        data={"metadata": "{not json"}  ,
    )
    assert bad_meta.status_code == 422
    assert bad_meta.json()["code"] == "REQUEST_INVALID"
    assert incoming(space) == []


def test_json_route_still_rejects_multipart_and_oversized_bodies(space):
    sid = subject(space)
    # 普通个人写接口继续只接受 JSON。
    multipart = space.client.post(
        f"/api/learning/subjects/{sid}/knowledge/confirm",
        files={"file": ("x.txt", SMALL, "text/plain")},
    )
    assert multipart.status_code == 415
    assert multipart.json()["code"] == "JSON_REQUIRED"
    oversized = space.client.post(
        "/api/learning/subjects",
        content=b'{"name": "' + b"y" * 70_000 + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "LIMIT_EXCEEDED"


def test_upload_requires_the_exact_source_boundary(space):
    sid = subject(space)
    for headers in ({"Host": "evil.invalid"}, {"Origin": "http://evil.invalid"}):
        response = space.client.post(
            f"/api/learning/subjects/{sid}/uploads",
            files={"file": ("notes.txt", SMALL, "text/plain")},
            data={"metadata": json.dumps({"purpose": "study_material"})},
            headers=headers,
        )
        assert response.status_code == 403, headers


def test_cross_space_subject_is_not_writable(space):
    from uuid import uuid4

    response = space.client.post(
        f"/api/learning/subjects/{uuid4()}/uploads",
        files={"file": ("notes.txt", SMALL, "text/plain")},
        data={"metadata": json.dumps({"purpose": "study_material"})},
    )
    assert response.status_code == 404
    assert incoming(space) == []


def test_unsupported_format_is_refused_before_storage(space):
    sid = subject(space)
    response = space.client.post(
        f"/api/learning/subjects/{sid}/uploads",
        files={"file": ("book.epub", b"PK\x03\x04data", "application/epub+zip")},
        data={"metadata": json.dumps({"purpose": "study_material"})},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "DOCUMENT_UNSUPPORTED_FORMAT"
    assert incoming(space) == []
    assert space.client.get(f"/api/learning/subjects/{sid}/materials").json()[
        "materials"
    ] == []
