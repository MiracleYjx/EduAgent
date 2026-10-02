"""T164 real HTTP ownership and persisted provider outcomes; no quality claims. TCR §17."""

from backend.app.services import image_understanding_service as understanding
from tests.contract.test_content_validation_api import _command, _question
from tests.contract.test_file_storage_api import files_api as _files_api
from tests.unit.ai.test_vision_provider import VisionStub, valid_result
from tests.unit.services.test_question_asset_service import png

files_api = _files_api


def test_image_understanding_auth_and_request_cannot_select_fake_inputs(files_api):
    client, _session, _files, _document, _users, headers = files_api
    question, _asset = _question(files_api)
    route = f"/api/questions/{question.id}/image-understanding"
    assert client.post(route, json={}).status_code == 401
    for denied in headers[1:]:
        assert client.post(route, json={}, headers=denied).status_code == 403
    for bad in [
        {"task": " "},
        {"images": []},
        {"file_path": "C:/private.png"},
        {"image_assessment": {"runs": []}},
        {"model": "client-picks-model"},
    ]:
        response = client.post(route, json=bad, headers=headers[0])
        assert response.status_code == 422, response.text
    view = client.get(
        f"/api/questions/{question.id}/image-assessment", headers=headers[0]
    ).json()
    assert view["run_no"] == 0


def test_unconfigured_model_failure_is_durable_and_manual_check_remains_available(
    files_api,
):
    client, _session, _files, _document, users, headers = files_api
    question, asset = _question(files_api)
    base = f"/api/questions/{question.id}"
    assert client.app.state.settings.vision_model is None
    response = client.post(base + "/image-understanding", json={}, headers=headers[0])
    assert response.status_code == 200, response.text
    view = response.json()
    run = view["current_run"]
    assert run["outcome"] == "technical_error" and run["result"] is None
    assert run["error"]["code"] == "VISION_PROVIDER_NOT_READY"
    assert run["requested_by"] == str(users[0].id)
    assert (
        run["provenance"]["provider_name"] is None
        and run["provenance"]["model"] is None
    )
    assert view["current_check"] is None and view["status"] == "pending"
    checked = client.post(
        base + "/image-manual-checks",
        json=_command(view, asset["id"]),
        headers=headers[0],
    )
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert (
        result["status"] == "confirmed"
        and result["current_check"]["run_id"] == run["id"]
    )
    assert result["assessment"]["runs"][0]["error"] == run["error"]


def test_missing_original_keeps_failure_and_never_calls_provider(
    monkeypatch, files_api
):
    client, _session, files, _document, users, headers = files_api
    question, asset = _question(files_api)
    path, _ = files.download(asset["file_id"], actor_id=users[0].id)
    path.unlink()

    def forbidden_factory(_settings):
        raise AssertionError("原图缺失不得调用模型")

    monkeypatch.setattr(understanding, "create_vision_provider", forbidden_factory)
    response = client.post(
        f"/api/questions/{question.id}/image-understanding",
        json={},
        headers=headers[0],
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["current_run"]["error"]["code"] == "FILE_MISSING"
    assert view["current_run"]["outcome"] == "technical_error"
    assert view["current_run"]["provenance"]["provider_name"] is None
    assert view["evidence_readable"] is False and view["current_check"] is None


def test_supported_whole_group_output_uses_authorized_ids_and_still_needs_teacher(
    monkeypatch, files_api
):
    client, _session, _files, _document, _users, headers = files_api
    question, first = _question(files_api)
    second_response = client.post(
        f"/api/questions/{question.id}/assets/upload",
        headers=headers[0],
        files={"file": ("second.png", png(), "image/png")},
    )
    assert second_response.status_code == 201
    second = second_response.json()
    provider = VisionStub()
    monkeypatch.setattr(
        understanding, "create_vision_provider", lambda _settings: provider
    )
    response = client.post(
        f"/api/questions/{question.id}/image-understanding",
        json={"task": "读取本题整组图的条件"},
        headers=headers[0],
    )
    assert response.status_code == 200, response.text
    view = response.json()
    run = view["current_run"]
    assert run["outcome"] == "completed" and run["error"] is None
    assert [item["asset_id"] for item in run["input_refs"]["images"]] == [
        first["id"],
        second["id"],
    ]
    assert run["result"]["conditions"][0]["asset_id"] == first["id"]
    assert run["result"]["conditions"][0]["condition_id"]
    assert (
        run["provenance"]["provider_name"] == "stub"
        and run["provenance"]["model"] == "fixture-vision"
    )
    assert view["current_check"] is None and view["requires_manual_review"] is True
    assert view["status"] == "pending"
    parts = provider.calls[0][1]["content"]
    assert len([part for part in parts if part["type"] == "image"]) == 2
    assert "base64" not in str(view)


def test_invalid_model_image_index_is_recorded_as_output_error(monkeypatch, files_api):
    client, _session, _files, _document, _users, headers = files_api
    question, _asset = _question(files_api)
    payload = valid_result()
    payload["conditions"][0]["image_index"] = 2
    provider = VisionStub(payload)
    monkeypatch.setattr(
        understanding, "create_vision_provider", lambda _settings: provider
    )
    response = client.post(
        f"/api/questions/{question.id}/image-understanding",
        json={},
        headers=headers[0],
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["current_run"]["outcome"] == "technical_error"
    assert view["current_run"]["error"]["code"] == "VISION_OUTPUT_INVALID"
    assert view["current_run"]["result"] is None
    assert view["current_run"]["provenance"]["provider_name"] == "stub"
    assert view["current_check"] is None
