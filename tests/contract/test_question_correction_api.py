"""T158 atomic correction and explicit batch confirmation. TCR §13."""

from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.ai.paper_extraction import PaperExtractor
from backend.app.domain.enums import PaperImportStatus, QuestionStatus
from backend.app.models import ExtractedQuestion, PaperImport, Question, QuestionAsset
from backend.app.services.paper_import_service import PaperImportRunner
from tests.contract.test_paper_import_api import files_api as _files_api
from tests.contract.test_paper_import_api import pause_background, upload
from tests.unit.ingestion.test_paper_extraction import Provider, question

files_api = _files_api


def pending(files_api, count=2):
    client, session, _files, _doc, _users, headers = files_api
    pause_background(client)
    initial = upload(files_api).json()
    runner = PaperImportRunner(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        settings=client.app.state.settings,
        extractor=PaperExtractor(
            Provider(
                [
                    {
                        "questions": [
                            question([1], question_number=str(n))
                            for n in range(1, count + 1)
                        ]
                    }
                ]
            )
        ),
    )
    runner.run(UUID(initial["id"]))
    value = client.get("/api/paper-imports/" + initial["id"], headers=headers[0]).json()
    assert value["status"] == "Pending Review", value
    return value


def patch(files_api, paper, qid, payload, header=0):
    client, *_rest, headers = files_api
    return client.patch(
        f"/api/paper-imports/{paper['id']}/questions/{qid}",
        json=payload,
        headers=headers[header],
    )


def commit(files_api, paper, ids):
    client, *_rest, headers = files_api
    return client.post(
        f"/api/paper-imports/{paper['id']}/commit",
        json={"question_ids": ids},
        headers=headers[0],
    )


def test_correction_persists_order_unknowns_and_atomic_batch(files_api):
    client, session, _files, _doc, users, headers = files_api
    paper = pending(files_api)
    first, second = [q["id"] for q in paper["questions"]]
    changed = patch(
        files_api,
        paper,
        second,
        {
            "score": "7.25",
            "assets": [],
            "order_index": 1,
            "analysis": "教师原文解析",
            "knowledge_points": [" 点A ", "点A"],
        },
    )
    assert changed.status_code == 200, changed.text
    listed = client.get(
        f"/api/paper-imports/{paper['id']}/questions", headers=headers[0]
    ).json()
    assert [(q["id"], q["order_index"]) for q in listed] == [(second, 1), (first, 2)]
    assert (
        listed[0]["knowledge_points"] == ["点A"] and listed[0]["source_regions"] is None
    )
    assert (
        patch(files_api, paper, first, {"assets": [], "score": "2.00"}).status_code
        == 200
    )
    invalid = commit(files_api, paper, [first, str(uuid4())])
    assert invalid.status_code == 404
    assert session.scalar(select(func.count()).select_from(Question)) == 0
    response = commit(files_api, paper, [first, second])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "Ready"
    assert all(
        q["completion_status"] == "needs_completion" and q["question_status"] == "Draft"
        for q in result["questions"]
    )
    identities = {
        q["extracted_question_id"]: q["question_id"] for q in result["questions"]
    }
    formal = session.get(Question, UUID(identities[second]))
    assert formal.analysis == "教师原文解析" and formal.knowledge_points == ["点A"]
    assert (
        formal.reference_answer is None
        and formal.scoring_rubric is None
        and str(formal.score) == "7.25"
    )
    assert (
        formal.source_type.value == "paper_imported"
        and formal.created_by == users[0].id
    )
    formal.analysis = "后续正式修订"
    session.commit()
    again = commit(files_api, paper, [second, first])
    assert (
        again.status_code == 200
        and session.scalar(select(func.count()).select_from(Question)) == 2
    )
    session.expire_all()
    assert session.get(Question, formal.id).analysis == "后续正式修订"
    assert session.get(ExtractedQuestion, UUID(second)).analysis == "教师原文解析"
    assert patch(files_api, paper, second, {"analysis": None}).status_code == 409


def test_partial_confirmation_rejection_and_invalid_request_rollback(files_api):
    client, session, _files, _doc, _users, headers = files_api
    paper = pending(files_api)
    first, second = [q["id"] for q in paper["questions"]]
    assert (
        patch(files_api, paper, first, {"score": "2", "assets": []}).status_code == 200
    )
    # An unready member rejects the entire explicitly selected batch.
    assert commit(files_api, paper, [first, second]).status_code == 409
    assert session.scalar(select(func.count()).select_from(Question)) == 0
    single = commit(files_api, paper, [first])
    assert single.status_code == 200 and single.json()["status"] == "Pending Review"
    assert patch(files_api, paper, second, {"action": "reject"}).status_code == 422
    assert (
        patch(
            files_api,
            paper,
            second,
            {"action": "reject", "correction_notes": "非试题页"},
        ).status_code
        == 200
    )
    assert (
        client.get(f"/api/paper-imports/{paper['id']}", headers=headers[0]).json()[
            "status"
        ]
        == "Ready"
    )
    assert commit(files_api, paper, [first, second]).status_code == 409
    second_paper = pending(files_api, 1)
    qid = second_paper["questions"][0]["id"]
    assert (
        patch(
            files_api,
            second_paper,
            qid,
            {"action": "reject", "correction_notes": "误识别"},
        ).status_code
        == 200
    )
    session.expire_all()
    assert (
        session.get(PaperImport, UUID(second_paper["id"])).status
        == PaperImportStatus.REJECTED
    )


def test_source_regions_assets_visibility_and_draft_mapping(files_api):
    client, session, _files, _doc, _users, headers = files_api
    paper = pending(files_api, 1)
    qid = paper["questions"][0]["id"]
    page = paper["pages"][0]
    assert (
        patch(
            files_api,
            paper,
            qid,
            {
                "score": "3",
                "source_regions": [
                    {"source_page_id": page["id"], "bbox": [0, 0, 99999, 20]}
                ],
            },
        ).status_code
        == 422
    )
    session.expire_all()
    assert session.get(ExtractedQuestion, UUID(qid)).score is None
    asset = client.post(
        f"/api/extracted-questions/{qid}/assets",
        headers=headers[0],
        json={
            "file_id": page["file_id"],
            "source_page_id": page["id"],
            "asset_type": "figure",
            "region": {"bbox": [0, 0, 100, 100]},
        },
    ).json()
    assert asset["student_visible"] is False
    asset["student_visible"] = True
    assert (
        patch(
            files_api,
            paper,
            qid,
            {
                "assets": [asset],
                "score": "3",
                "reference_answer": "人工核对答案",
                "scoring_rubric": "人工评分标准",
            },
        ).status_code
        == 200
    )
    assert patch(files_api, paper, qid, {"source_page_ids": []}).status_code == 422
    assert (
        patch(
            files_api,
            paper,
            qid,
            {"source_page_ids": [str(uuid4())], "assets": [], "source_regions": None},
        ).status_code
        == 422
    )
    assert (
        patch(files_api, paper, qid, {"content": "越权"}, header=1).status_code == 403
    )
    response = commit(files_api, paper, [qid])
    assert response.status_code == 200, response.text
    assert response.json()["questions"][0]["completion_status"] == "needs_completion"
    session.expire_all()
    formal_asset = session.get(QuestionAsset, UUID(asset["id"]))
    assert formal_asset.student_visible is True and (
        formal_asset.width,
        formal_asset.height,
    ) == (100, 100)
    assert formal_asset._file_path is None and formal_asset.storage_path
    assert formal_asset.question.status == QuestionStatus.DRAFT
    assert formal_asset.question.image_assessment is None
    assert (
        client.get("/api/files/" + asset["file_id"], headers=headers[2]).status_code
        == 403
    )


def test_options_missing_files_and_forced_transaction_failure(files_api, monkeypatch):
    _client, session, files, _doc, _users, _headers = files_api
    paper = pending(files_api, 1)
    qid = paper["questions"][0]["id"]
    assert (
        patch(
            files_api,
            paper,
            qid,
            {"question_type": "SINGLE_CHOICE", "score": "1", "assets": []},
        ).status_code
        == 200
    )
    assert commit(files_api, paper, [qid]).status_code == 409
    assert (
        patch(
            files_api,
            paper,
            qid,
            {"options": {"B": "第二项", "A": "第一项"}, "reference_answer": "B"},
        ).status_code
        == 200
    )
    from sqlalchemy.exc import SQLAlchemyError

    from backend.app.services.question_correction_service import (
        QuestionCorrectionService,
    )

    original = QuestionCorrectionService._commit

    def fail(self):
        self.session.flush()
        raise SQLAlchemyError("forced rollback")

    monkeypatch.setattr(QuestionCorrectionService, "_commit", fail)
    assert commit(files_api, paper, [qid]).status_code == 503
    assert session.scalar(select(func.count()).select_from(Question)) == 0
    monkeypatch.setattr(QuestionCorrectionService, "_commit", original)
    page_file = files.resolve_path(
        session.get(PaperImport, UUID(paper["id"])).pages[0].image_path
    )
    page_file.unlink()
    assert commit(files_api, paper, [qid]).status_code == 404
    session.expire_all()
    assert session.get(ExtractedQuestion, UUID(qid)).question_id is None


def test_current_image_evidence_transfers_once_and_input_revision_prevents_reuse(
    files_api,
):
    from datetime import UTC, datetime

    from backend.app.schemas.image_assessment import (
        ImageAssessment,
        ImageInput,
        ImageInputRefs,
        ImageManualCheck,
    )
    from backend.app.services.question_correction_service import IMAGE_TEXT_FIELDS

    client, session, _files, _doc, users, headers = files_api
    paper = pending(files_api, 2)
    for number, item in enumerate(paper["questions"]):
        qid = item["id"]
        page = paper["pages"][0]
        asset = client.post(
            f"/api/extracted-questions/{qid}/assets",
            headers=headers[0],
            json={
                "file_id": page["file_id"],
                "source_page_id": page["id"],
                "asset_type": "figure",
                "region": {"bbox": [0, 0, 100, 100]},
            },
        ).json()
        assert (
            patch(
                files_api,
                paper,
                qid,
                {
                    "score": "3",
                    "reference_answer": "已核对答案",
                    "scoring_rubric": "正确得 3 分",
                },
            ).status_code
            == 200
        )
        session.expire_all()
        staged = session.get(ExtractedQuestion, UUID(qid))
        assessment = ImageAssessment.model_validate(staged.image_assessment)
        refs = ImageInputRefs(
            text_fields=[*IMAGE_TEXT_FIELDS, "caption"],
            images=[
                ImageInput(
                    asset_id=UUID(asset["id"]),
                    file_id=asset["file_id"],
                    image_index=1,
                    asset_type="figure",
                    source_page_id=UUID(page["id"]),
                    region={"bbox": [0, 0, 100, 100]},
                    width=100,
                    height=100,
                    mime_type="image/png",
                )
            ],
        )
        check = ImageManualCheck(
            id=uuid4(),
            check_no=1,
            context_revision=assessment.context_revision,
            run_no=0,
            run_id=None,
            input_refs=refs,
            status="confirmed",
            confirmed_conditions=[],
            image_findings=[
                {
                    "asset_id": asset["id"],
                    "finding": "no_conditions_needed",
                    "reason": "测试夹具：教师判定此图无额外必要条件",
                }
            ],
            issues=[],
            issue_resolutions=[],
            teacher_id=users[0].id,
            checked_at=datetime.now(UTC),
            explanation="测试夹具核对",
        )
        assessment.manual_checks = [check]
        staged.image_assessment = assessment.model_dump(mode="json")
        session.commit()
        # An unchanged amount is the same input; classifications and original number aren't image context.
        assert (
            patch(
                files_api,
                paper,
                qid,
                {
                    "score": "3.00",
                    "knowledge_points": ["新增分类"],
                    "question_number": "01",
                },
            ).status_code
            == 200
        )
        session.expire_all()
        assert (
            session.get(ExtractedQuestion, UUID(qid)).image_assessment[
                "context_revision"
            ]
            == assessment.context_revision
        )
        if number:
            assert (
                patch(files_api, paper, qid, {"content": "临时修订"}).status_code == 200
            )
            assert (
                patch(files_api, paper, qid, {"content": "解释跨页问题"}).status_code
                == 200
            )
        response = commit(files_api, paper, [qid])
        assert response.status_code == 200, response.text
        session.expire_all()
        formal = session.get(
            Question, UUID(response.json()["questions"][0]["question_id"])
        )
        if number:
            assert formal.image_assessment is None
            assert (
                response.json()["questions"][0]["completion_status"]
                == "needs_completion"
            )
        else:
            binding = ImageAssessment.model_validate(formal.image_assessment)
            assert binding.imported_review.source_ref.check_id == check.id
            assert binding.runs == [] and binding.manual_checks == []
            assert response.json()["questions"][0]["completion_status"] == "complete"
        source = session.get(ExtractedQuestion, UUID(qid))
        assert source.image_assessment["manual_checks"][0]["teacher_id"] == str(
            users[0].id
        )
        assert (
            source.image_assessment["manual_checks"][0]["checked_at"]
            == check.model_dump(mode="json")["checked_at"]
        )


def test_legacy_order_marker_preserved_and_explicit_reorder_invalidates(files_api):
    _client, session, _files, _doc, _users, _headers = files_api
    paper = pending(files_api, 1)
    qid = paper["questions"][0]["id"]
    options = {"C": "7", "A": "5", "D": "8", "B": "6"}
    assert (
        patch(
            files_api, paper, qid, {"options": options, "score": "2", "assets": []}
        ).status_code
        == 200
    )
    session.expire_all()
    source = session.get(ExtractedQuestion, UUID(qid))
    source.order_preserved = False
    source.image_assessment = {"context_revision": 0}
    session.commit()
    unchanged = patch(
        files_api, paper, qid, {"options": options, "analysis": "只改解析"}
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["order_preserved"] is False
    revision = unchanged.json()["image_assessment"]["context_revision"]
    reordered = {"B": "6", "D": "8", "A": "5", "C": "7"}
    changed = patch(files_api, paper, qid, {"options": reordered})
    assert changed.status_code == 200
    assert list(changed.json()["options"]) == list(reordered)
    assert changed.json()["order_preserved"] is True
    assert changed.json()["image_assessment"]["context_revision"] == revision + 1
    back = patch(files_api, paper, qid, {"options": options})
    assert back.json()["image_assessment"]["context_revision"] == revision + 2
    denied = patch(files_api, paper, qid, {"order_preserved": True})
    assert denied.status_code == 422
    session.expire_all()
    session.get(ExtractedQuestion, UUID(qid)).order_preserved = False
    session.commit()
    response = commit(files_api, paper, [qid])
    assert response.status_code == 200
    formal_id = UUID(response.json()["questions"][0]["question_id"])
    session.expire_all()
    formal = session.get(Question, formal_id)
    assert formal.order_preserved is False
    assert commit(files_api, paper, [qid]).status_code == 200
    session.expire_all()
    assert session.get(Question, formal_id).order_preserved is False
