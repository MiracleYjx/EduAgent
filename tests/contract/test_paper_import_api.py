"""T157 HTTP upload/background/ownership with actual database and files. TCR §13."""

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.ai.paper_extraction import PaperExtractor
from backend.app.models import DocumentChunk, PaperImport
from backend.app.services.paper_import_service import PaperImportRunner
from tests.contract.test_file_storage_api import files_api as _files_api
from tests.unit.ingestion.test_paper_extraction import Provider, question

files_api = _files_api
SAMPLES = (
    Path(__file__).resolve().parents[2] / "benchmark/corpus/v2-draft-20261001/inputs"
)


def upload(files_api, name="paper_text.pdf", header=0):
    client, _session, _files, doc, _users, headers = files_api
    return client.post(
        "/api/paper-imports",
        headers=headers[header],
        data={"course_id": str(doc.course_id)},
        files={"file": (name, (SAMPLES / name).read_bytes(), "application/pdf")},
    )


def pause_background(client):
    ids = []
    client.app.state.paper_import_runner = SimpleNamespace(run=ids.append)
    return ids


def test_upload_saved_first_real_worker_progress_and_file_permissions(files_api):
    client, session, _files, _doc, _users, headers = files_api
    scheduled = pause_background(client)
    response = upload(files_api)
    assert response.status_code == 201, response.text
    received = response.json()
    assert received["status"] == "Uploaded" and received["pages"] == []
    assert scheduled == [UUID(received["id"])]
    assert "storage_path" not in response.text
    raw = client.get("/api/files/" + received["original_file_id"], headers=headers[0])
    assert raw.content == (SAMPLES / "paper_text.pdf").read_bytes()
    runner = PaperImportRunner(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        settings=client.app.state.settings,
        extractor=PaperExtractor(Provider([{"questions": [question([1])]}])),
    )
    runner.run(UUID(received["id"]))
    result = client.get(
        "/api/paper-imports/" + received["id"], headers=headers[0]
    ).json()
    assert result["status"] == "Pending Review"
    assert result["parsed_page_count"] == 1 and result["question_count"] == 1
    assert result["pages"][0]["ocr_text"] is None
    assert result["questions"][0]["order_index"] == 1
    assert result["questions"][0]["assets"] is None
    assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert (
        client.get(
            "/api/paper-imports/" + received["id"], headers=headers[1]
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/files/" + result["pages"][0]["file_id"], headers=headers[2]
        ).status_code
        == 403
    )
    listing = client.get(
        "/api/paper-imports",
        params={"course_id": result["course_id"]},
        headers=headers[0],
    )
    assert listing.status_code == 200 and listing.json()[0]["id"] == result["id"]


def test_scan_disabled_failure_retains_original_and_actual_pages(files_api):
    client, session, _files, _doc, _users, headers = files_api
    pause_background(client)
    response = upload(files_api, "paper_scan.pdf")
    identity = UUID(response.json()["id"])
    runner = PaperImportRunner(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        settings=client.app.state.settings,
    )
    runner.run(identity)
    result = client.get(f"/api/paper-imports/{identity}", headers=headers[0]).json()
    assert (
        result["status"] == "Failed"
        and result["error_code"] == "OCR_PROVIDER_NOT_READY"
    )
    assert result["parsed_page_count"] == 1 and result["question_count"] == 0
    assert result["pages"][0]["ocr_text"] is None
    session.expire_all()
    imported = session.get(PaperImport, identity)
    assert imported.document.status.value == "Ready"


def test_rejected_uploads_do_not_create_import_and_worker_restart_is_truthful(
    files_api,
):
    client, session, _files, doc, _users, headers = files_api
    pause_background(client)
    for header in (1, 2):
        assert upload(files_api, header=header).status_code == 403
    bad = client.post(
        "/api/paper-imports",
        headers=headers[0],
        data={"course_id": str(doc.course_id)},
        files={"file": ("bad.pdf", b"broken", "application/pdf")},
    )
    assert (
        bad.status_code == 422 and bad.json()["detail"]["code"] == "PAPER_PARSE_FAILED"
    )
    over = upload(files_api, "workload_51_pages.pdf")
    assert (
        over.status_code == 422
        and over.json()["detail"]["code"] == "PAPER_TOO_MANY_PAGES"
    )
    assert session.scalar(select(func.count()).select_from(PaperImport)) == 0
    accepted = upload(files_api).json()
    runner = PaperImportRunner(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        settings=client.app.state.settings,
    )
    assert runner.recover_interrupted() == 1
    result = client.get(
        "/api/paper-imports/" + accepted["id"], headers=headers[0]
    ).json()
    assert result["status"] == "Failed" and result["error_code"] == "PAPER_INTERRUPTED"
    assert result["page_count"] is None and result["pages"] == []
    assert client.get("/api/paper-imports/" + accepted["id"]).status_code == 401
