"""T163 authentic HTTP commands and immutable teacher evidence. TCR §17."""

from uuid import UUID, uuid4

from backend.app.models import Question
from backend.app.services.question_service import QuestionService
from tests.contract.test_file_storage_api import files_api as _files_api
from tests.unit.services.test_question_asset_service import png

files_api = _files_api


def _question(files_api):
    client, session, _files, document, users, headers = files_api
    question = QuestionService(session).create_question(
        document.course_id,
        "SHORT_ANSWER",
        "请根据题图作答",
        created_by=users[0].id,
    )
    response = client.post(
        f"/api/questions/{question.id}/assets/upload",
        headers=headers[0],
        files={"file": ("own.png", png(), "image/png")},
    )
    assert response.status_code == 201, response.text
    return question, response.json()


def _command(view, asset_id, **changes):
    command = {
        "expected_context_revision": view["context_revision"],
        "expected_run_no": view["run_no"],
        "expected_check_no": view["check_no"],
        "status": "confirmed",
        "image_findings": [
            {
                "asset_id": asset_id,
                "finding": "no_conditions_needed",
                "reason": "已对照原图，当前示意图无额外数值或关系。",
            }
        ],
        "confirmed_conditions": [],
        "issues": [],
        "issue_resolutions": [],
        "explanation": "教师实际阅读本题原图，记录无需新增条件的依据。",
    }
    return command | changes


def test_image_assessment_is_teacher_only_and_has_no_fabricated_check(files_api):
    client, _session, _files, _document, _users, headers = files_api
    question, _asset = _question(files_api)
    url = f"/api/questions/{question.id}/image-assessment"
    assert client.get(url).status_code == 401
    for denied in headers[1:]:
        assert client.get(url, headers=denied).status_code == 403
    response = client.get(url, headers=headers[0])
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["status"] == "pending" and view["current_check"] is None
    assert view["assessment"]["manual_checks"] == []
    assert view["run_no"] == 0 and view["check_no"] == 0
    assert view["input_refs"]["images"][0]["image_index"] == 1
    assert "file_path" not in str(view) and "storage_path" not in str(view)


def test_human_command_records_real_teacher_time_and_rejects_replay(files_api):
    client, session, _files, _document, users, headers = files_api
    question, asset = _question(files_api)
    url = f"/api/questions/{question.id}/image-assessment"
    view = client.get(url, headers=headers[0]).json()
    payload = _command(view, asset["id"])
    route = f"/api/questions/{question.id}/image-manual-checks"
    response = client.post(route, json=payload, headers=headers[0])
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["status"] == "confirmed"
    check = saved["current_check"]
    assert check["teacher_id"] == str(users[0].id) and check["checked_at"].endswith("Z")
    assert check["input_refs"]["images"][0]["asset_id"] == asset["id"]
    assert check["run_id"] is None and check["run_no"] == 0 and check["check_no"] == 1
    session.expire_all()
    stored = session.get(Question, UUID(question.id)).image_assessment
    assert stored["manual_checks"][0]["id"] == check["id"]
    repeated = client.post(route, json=payload, headers=headers[0])
    assert repeated.status_code == 409
    assert repeated.json()["detail"]["code"] == "IMAGE_ASSESSMENT_STALE"
    assert (
        len(client.get(url, headers=headers[0]).json()["assessment"]["manual_checks"])
        == 1
    )


def test_teacher_cannot_replace_machine_history_or_spoof_identity(files_api):
    client, _session, _files, _document, users, headers = files_api
    question, asset = _question(files_api)
    base = f"/api/questions/{question.id}"
    view = client.get(base + "/image-assessment", headers=headers[0]).json()
    payload = _command(view, asset["id"])
    for forged in [
        {"teacher_id": str(users[1].id)},
        {"checked_at": "2020-01-01T00:00:00Z"},
        {"runs": []},
        {"image_assessment": {"context_revision": 0}},
        {"imported_review": None},
    ]:
        response = client.post(
            base + "/image-manual-checks", json=payload | forged, headers=headers[0]
        )
        assert response.status_code == 422, response.text
    assert (
        client.put(base + "/image-assessment", json={}, headers=headers[0]).status_code
        == 405
    )
    assert (
        client.post(
            base + "/image-understanding/finish", json={}, headers=headers[0]
        ).status_code
        == 404
    )
    assert (
        client.get(base + "/image-assessment", headers=headers[0]).json()["check_no"]
        == 0
    )


def test_validation_history_permissions_empty_and_missing_report_are_distinct(
    files_api,
):
    client, _session, _files, document, users, headers = files_api
    question = QuestionService(_session).create_question(
        document.course_id,
        "SHORT_ANSWER",
        "无核验历史的真实草稿",
        created_by=users[0].id,
    )
    route = f"/api/questions/{question.id}/validations"
    assert client.get(route).status_code == 401
    for denied in headers[1:]:
        assert client.get(route, headers=denied).status_code == 403
    response = client.get(route, headers=headers[0])
    assert response.status_code == 200 and response.json() == []
    missing = client.get(route + "/" + str(uuid4()), headers=headers[0])
    assert missing.status_code == 404


def test_manual_check_requires_current_whole_group_and_readable_original(files_api):
    client, _session, files, _document, users, headers = files_api
    question, first = _question(files_api)
    response = client.post(
        f"/api/questions/{question.id}/assets/upload",
        headers=headers[0],
        files={"file": ("second.png", png(), "image/png")},
    )
    assert response.status_code == 201
    second = response.json()
    base = f"/api/questions/{question.id}"
    view = client.get(base + "/image-assessment", headers=headers[0]).json()
    partial = client.post(
        base + "/image-manual-checks",
        json=_command(view, first["id"]),
        headers=headers[0],
    )
    assert partial.status_code == 422, partial.text
    assert (
        client.get(base + "/image-assessment", headers=headers[0]).json()["check_no"]
        == 0
    )
    path, _ = files.download(second["file_id"], actor_id=users[0].id)
    path.unlink()
    payload = _command(view, first["id"])
    payload["image_findings"].append(
        {
            "asset_id": second["id"],
            "finding": "no_conditions_needed",
            "reason": "第二图也无额外条件。",
        }
    )
    failed = client.post(
        base + "/image-manual-checks", json=payload, headers=headers[0]
    )
    assert (
        failed.status_code == 404 and failed.json()["detail"]["code"] == "FILE_MISSING"
    )
    assert (
        client.get(base + "/image-assessment", headers=headers[0]).json()["check_no"]
        == 0
    )


def test_current_report_disposition_appends_real_comment_without_rewriting_result(
    files_api,
):
    from backend.app.domain.enums import DocumentStatus, QuestionStatus
    from backend.app.models import DocumentChunk, QuestionRevisionComment
    from backend.app.schemas.content_validation import (
        CHECK_KINDS,
        ValidationCheck,
        ValidationEvidence,
        ValidationInputRefs,
        ValidationOutput,
    )
    from backend.app.services.content_validation_service import ContentValidationService

    client, session, _files, document, users, headers = files_api
    created = QuestionService(session).create_question(
        document.course_id,
        "SHORT_ANSWER",
        "需人工处置的真实候选",
        created_by=users[0].id,
        reference_answer="owned original",
        scoring_rubric="核对原文中的实际条件",
        score="1",
    )
    question = session.get(Question, UUID(created.id))
    question.status = QuestionStatus.PENDING_REVIEW
    document.status = DocumentStatus.READY
    chunk = DocumentChunk(
        document_id=document.id,
        course_id=document.course_id,
        knowledge_base_id=document.knowledge_base_id,
        chunk_index=0,
        content="owned original",
        chunk_metadata={"location": "page 1"},
    )
    session.add(chunk)
    session.commit()
    evidence = ValidationEvidence(
        evidence_id=uuid4(),
        kind="chunk",
        source_id=chunk.id,
        source_data={
            "chunk_id": str(chunk.id),
            "document_id": str(document.id),
            "course_id": str(document.course_id),
            "source_file": document.original_filename,
            "location": "page 1",
            "content_snapshot": chunk.content,
        },
    )
    service = ContentValidationService(
        session, root=client.app.state.settings.storage_root
    )
    report = service.start_validation(
        question.id,
        actor_id=users[0].id,
        input_refs=ValidationInputRefs(
            fields=[
                "type",
                "content",
                "options",
                "reference_answer",
                "scoring_rubric",
                "analysis",
                "score",
            ],
            evidence=[evidence],
        ),
        executor_name="contract_fixture",
    )
    ended = service.finish_validation(
        report.id,
        actor_id=users[0].id,
        output=ValidationOutput(
            checks=[
                ValidationCheck(
                    kind=kind, verdict="needs_review", reason="待教师补齐实际条件", evidence_refs=[evidence.evidence_id]
                )
                for kind in sorted(CHECK_KINDS)
            ],
            issues=[],
        ),
    )
    route = f"/api/questions/{question.id}/validations/{report.id}"
    original = client.get(route, headers=headers[0])
    assert original.status_code == 200 and original.json()["outcome"] == "failed"
    payload = {
        "check_kind": "condition_sufficiency",
        "action": "request_revision",
        "reason": "教师要求补充原题缺失的条件，保留真实意见。",
    }
    assert (
        client.post(
            route + "/dispositions", json=payload, headers=headers[1]
        ).status_code
        == 403
    )
    forged = client.post(
        route + "/dispositions",
        json=payload | {"handled_by": str(users[1].id)},
        headers=headers[0],
    )
    assert forged.status_code == 422
    response = client.post(route + "/dispositions", json=payload, headers=headers[0])
    assert response.status_code == 200, response.text
    view = response.json()
    event = view["manual_dispositions"][0]
    assert event["handled_by"] == str(users[0].id)
    assert (
        view["checks"] == original.json()["checks"] and view["outcome"] == ended.outcome
    )
    session.expire_all()
    comment = session.get(QuestionRevisionComment, UUID(event["revision_comment_id"]))
    assert comment.question_id == question.id and comment.commented_by == users[0].id
    assert comment.comment == payload["reason"]
    QuestionService(session).update_question(
        question.id, content="已改动的题目", teacher_id=users[0].id
    )
    rejected = client.post(route + "/dispositions", json=payload, headers=headers[0])
    assert (
        rejected.status_code == 409
        and rejected.json()["detail"]["code"] == "CONTENT_VALIDATION_STALE"
    )
    assert len(client.get(route, headers=headers[0]).json()["manual_dispositions"]) == 1
