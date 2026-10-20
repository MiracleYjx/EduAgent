"""Personal content uses real migrated PG, verified Test roots and managed bytes."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api.subjects import get_subject_service
from backend.app.core.bootstrap import BootstrapContext, create_bootstrap_app
from backend.app.models import Document, Question
from backend.app.models.practice import PracticeItem, PracticeSession
from backend.app.models.question_revision import (
    QuestionRevision,
    question_revision_assets,
)
from backend.app.models.subject import Subject
from backend.app.schemas.settings import (
    BootstrapOptions,
    BootstrapSettings,
    RuntimeKind,
)
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.question_asset_access import PersonalContentAccess
from backend.app.services.subject_service import SubjectService
from tests.integration.test_personal_learning_foundation import migrate
from tests.postgres_helpers import (
    isolated_postgres_schema,
    isolated_test_resource_profile,
)
from tests.support.learning_content import registration, seed_content


@pytest.fixture
def content_space(tmp_path):
    with (isolated_test_resource_profile((tmp_path / "space").resolve()) as profile,
          isolated_postgres_schema() as (engine, schema)):
        migrate(engine, "head")
        ids = seed_content(engine, profile)
        service = SubjectService(sessionmaker(engine), storage_root=profile.storage_root)
        service.profile = profile
        app = create_bootstrap_app(context=BootstrapContext(settings=BootstrapSettings(
            environment=profile.environment, environment_id=profile.environment_id,
            runtime_kind=RuntimeKind.PYTHON, config_root=profile.config_root),
            bind=BootstrapOptions(port=8000), static_root=None))
        app.dependency_overrides[get_subject_service] = lambda: service
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            yield SimpleNamespace(engine=engine, schema=schema, profile=profile, ids=ids, app=app, client=client)


def base(space):
    ids = space.ids
    return f'/api/learning/subjects/{ids["subject"]}/practices/{ids["practice"]}/items/{ids["item"]}'


def test_answering_payload_and_direct_assets_do_not_expose_explanations(content_space):
    s = content_space
    response = s.client.get(base(s) + "/content")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["phase"] == "answering" and not value["automatic_grading_ready"]
    for private in ("reference_answer", "scoring_rubric", "scoring_input", "explanation_blocks", "解析", str(s.ids["assets"][6]), str(s.ids["assets"][7])):
        assert private not in response.text
    assert [b["type"] for b in value["stem_blocks"]] == ["text", "image", "text", "image", "formula", "table", "original"]
    for index in (0, 1, 2, 3, 4, 5, 8):
        image = s.client.get(base(s) + "/assets/" + str(s.ids["assets"][index]))
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert image.headers["cache-control"] == "no-store"
    assert s.client.get(base(s) + "/assets/" + str(s.ids["assets"][6])).status_code == 404
    assert s.client.get(base(s) + "/content?phase=review").json() == value


def test_database_submission_unlocks_only_actual_revision_explanation(content_space):
    s = content_space
    with s.engine.begin() as c:
        c.execute(text("UPDATE practice_sessions SET state='submitted',submitted_at=now() WHERE id=:id"), {"id": s.ids["practice"]})
    content = s.client.get(base(s) + "/content").json()
    assert content["phase"] == "review" and len(content["explanation_blocks"]) == 5
    assert s.client.get(base(s) + "/assets/" + str(s.ids["assets"][6])).status_code == 200
    assert s.client.get(base(s) + "/assets/" + str(uuid4())).status_code == 404


def test_subject_item_and_source_policy_are_not_client_grants(content_space):
    s = content_space
    path = base(s) + "/assets/" + str(s.ids["assets"][0])
    for invalid in (path.replace(str(s.ids["subject"]), str(s.ids["other_subject"])),
                    path.replace(str(s.ids["item"]), str(uuid4())),
                    path.replace(str(s.ids["practice"]), str(uuid4()))):
        assert s.client.get(invalid).status_code == 404
    for headers in ({"Host": "evil.invalid"}, {"Origin": "http://evil.invalid"}):
        assert s.client.get(path, headers=headers).status_code == 403
    assert s.client.get("/api/settings/").status_code == 401


def test_history_freezes_revision_asset_and_source_and_ignores_new_default(content_space):
    s = content_space
    for statement, params in (
        ("UPDATE question_assets SET caption='changed' WHERE id=:id", {"id": s.ids["assets"][0]}),
        ("UPDATE question_origins SET source_location='{}' WHERE id=:id", {"id": s.ids["origin"]}),
        ("UPDATE question_revisions SET stem_blocks='[]' WHERE id=:id", {"id": s.ids["revision"]}),
        ("DELETE FROM question_revision_assets WHERE revision_id=:id", {"id": s.ids["revision"]}),
    ):
        with pytest.raises(DBAPIError), s.engine.begin() as c:
            c.execute(text(statement), params)
    with Session(s.engine) as session, session.begin():
        new = QuestionRevision(question_id=s.ids["question"], subject_id=s.ids["subject"], revision=2,
            question_type="short_answer", stem_blocks=[{"type": "text", "text": "new revision"}],
            options=[], explanation_blocks=[], grounding="general_knowledge", admission_state="needs_validation")
        session.add(new)
        session.flush()
        session.execute(text("UPDATE questions SET current_revision_id=:r WHERE id=:q"), {"r": new.id, "q": s.ids["question"]})
    assert s.client.get(base(s) + "/content").json()["revision_id"] == str(s.ids["revision"])


def test_missing_tampered_and_other_space_files_fail_without_path_disclosure(content_space, tmp_path):
    s = content_space
    image = s.profile.storage_root / "content/image-0.png"
    image.write_bytes(b"tampered")
    path = base(s) + "/assets/" + str(s.ids["assets"][0])
    response = s.client.get(path)
    assert response.status_code == 409 and response.json()["code"] == "FILE_INTEGRITY_MISMATCH"
    assert str(s.profile.storage_root) not in response.text
    image.unlink()
    assert s.client.get(path).status_code == 404
    assert "CONTENT_RESOURCE_UNAVAILABLE" in s.client.get(base(s) + "/content").json()["issues"]
    with isolated_test_resource_profile((tmp_path / "other-space").resolve()) as other:
        other.storage_root.mkdir(parents=True)
        with Session(s.engine) as session:
            access = PersonalContentAccess(session, other)
            with pytest.raises(FileStorageError, match="已登记文件缺失"):
                access.asset_path(s.ids["subject"], s.ids["practice"], s.ids["item"], s.ids["assets"][1])


def test_docx_media_and_formula_are_actual_source_objects_with_resolved_anchors(content_space):
    from zipfile import ZipFile

    from lxml import etree

    s = content_space
    response = s.client.get(base(s) + "/content").json()
    assert len(response["asset_sources"]) == 7
    with ZipFile(s.profile.storage_root / "content/source.docx") as package:
        root = etree.fromstring(package.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
              "m": "http://schemas.openxmlformats.org/officeDocument/2006/math"}
        assert root.xpath("//m:sSup/m:e/m:r/m:t/text()", namespaces=ns) == ["x"]
        assert root.xpath("//m:sSup/m:sup/m:r/m:t/text()", namespaces=ns) == ["2"]
        media = [package.read(name) for name in package.namelist() if name.startswith("word/media/")]
        for index, asset_id in enumerate(s.ids["assets"]):
            assert (s.profile.storage_root / f"content/image-{index}.png").read_bytes() in media
            if str(asset_id) in response["asset_sources"]:
                source = response["asset_sources"][str(asset_id)]
                assert source["document_id"] == str(s.ids["document"])
                anchor = source["anchor"].split("#", 1)[1]
                assert len(root.xpath("/w:document/w:body/w:" + anchor.replace("/", "/w:"), namespaces=ns)) == 1


def test_whole_original_byte_alias_is_denied_during_answering(content_space):
    s = content_space
    data = (s.profile.storage_root / "content/image-0.png").read_bytes()
    with Session(s.engine) as session, session.begin():
        session.add(Document(subject_id=s.ids["other_subject"], original_filename="original-page.png", file_format="png",
            purpose="paper_source", status="Uploaded", storage_path="content/image-0.png", file_metadata=registration(data), retryable=False))
    path = base(s) + "/assets/" + str(s.ids["assets"][0])
    assert s.client.get(path).status_code == 403
    assert not s.client.get(base(s) + "/content").json()["automatic_grading_ready"]


def test_database_rejects_cross_question_revision_asset_binding(content_space):
    s = content_space
    with Session(s.engine) as session, session.begin():
        question = Question(subject_id=s.ids["subject"], knowledge_points=[], status="Draft")
        session.add(question)
        session.flush()
        revision = QuestionRevision(question_id=question.id, subject_id=s.ids["subject"], revision=1,
            question_type="short_answer", stem_blocks=[{"type": "text", "text": "pure text"}], options=[],
            explanation_blocks=[], grounding="general_knowledge", admission_state="eligible")
        session.add(revision)
        session.flush()
        revision_id = revision.id
    with pytest.raises(DBAPIError), s.engine.begin() as c:
        c.execute(question_revision_assets.insert().values(revision_id=revision_id, asset_id=s.ids["assets"][0]))


def test_persisted_pure_text_uses_same_reader_and_never_exposes_scoring_input(content_space):
    s = content_space
    with Session(s.engine) as session, session.begin():
        q = Question(subject_id=s.ids["subject"], knowledge_points=[], status="Draft")
        session.add(q)
        session.flush()
        revision = QuestionRevision(question_id=q.id, subject_id=s.ids["subject"], revision=1,
            question_type="short_answer", stem_blocks=[{"type": "text", "text": "解释概念"}], options=[],
            explanation_blocks=[{"type": "text", "text": "PRIVATE EXPLANATION"}], reference_answer="PRIVATE ANSWER", scoring_rubric="PRIVATE RUBRIC",
            grounding="general_knowledge", admission_state="eligible")
        session.add(revision)
        practice = PracticeSession(subject_id=s.ids["subject"], state="preview", conditions={})
        session.add(practice)
        session.flush()
        item = PracticeItem(session_id=practice.id, subject_id=s.ids["subject"], position=1, question_id=q.id,
            question_revision_id=revision.id, fixed_max_score=10, scoring_input={"answer": "PRIVATE ANSWER"}, knowledge_point_ids=[])
        session.add(item)
        session.flush()
        practice.state = "in_progress"
        path = f'/api/learning/subjects/{s.ids["subject"]}/practices/{practice.id}/items/{item.id}/content'
    response = s.client.get(path)
    assert response.status_code == 200
    assert response.json()["stem_blocks"] == [{"type": "text", "text": "解释概念"}]
    assert response.json()["asset_sources"] == {} and response.json()["automatic_grading_ready"]
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("scenario", ["unknown_source", "wrong_source", "unbound", "unsupported_mime", "forged_signature"])
def test_unconfirmed_or_invalid_registered_media_never_grants_automatic_grading(tmp_path, scenario):
    def configure(session, question, document, assets, revision):
        revision.stem_blocks = [{"type": "image", "asset_id": str(assets[0].id), "alt": "critical figure"}]
        revision.explanation_blocks = []
        revision.admission_state = "eligible"
        if scenario == "unknown_source":
            assets[0].source_document_id = assets[0].source_anchor = None
        elif scenario == "wrong_source":
            other = Subject(name="other")
            session.add(other)
            session.flush()
            source = Document(subject_id=other.id, original_filename="other.txt", file_format="txt", purpose="paper_source", status="Uploaded", retryable=False)
            session.add(source)
            session.flush()
            assets[0].source_document_id = source.id
        elif scenario == "unbound":
            session.execute(question_revision_assets.delete().where(question_revision_assets.c.revision_id == revision.id))
        elif scenario == "unsupported_mime":
            assets[0].file_metadata = assets[0].file_metadata | {"media_type": "image/svg+xml"}
        else:
            data = b"<html>not an image</html>"
            (profile.storage_root / assets[0].storage_path).write_bytes(data)
            assets[0].file_metadata = registration(data)
    with (isolated_test_resource_profile((tmp_path / "space").resolve()) as profile,
          isolated_postgres_schema() as (engine, _)):
        migrate(engine, "head")
        ids = seed_content(engine, profile, configure=configure)
        with Session(engine) as session:
            access = PersonalContentAccess(session, profile)
            view = access.view(ids["subject"], ids["practice"], ids["item"])
            assert not view.automatic_grading_ready
            if scenario != "unknown_source":
                assert "CONTENT_RESOURCE_UNAVAILABLE" in view.issues
                from backend.app.services.subject_service import SubjectError
                with pytest.raises(SubjectError):
                    access.asset_path(ids["subject"], ids["practice"], ids["item"], ids["assets"][0])
            else:
                assert "CONTENT_SOURCE_UNCONFIRMED" in view.issues


def test_upgrade_from_0026_retains_registered_bytes_and_existing_history(tmp_path):
    import json
    from io import BytesIO

    from PIL import Image

    from tests.integration.test_personal_learning_foundation import seed

    with (isolated_test_resource_profile((tmp_path / "space").resolve()) as profile,
          isolated_postgres_schema() as (engine, _)):
        migrate(engine, "0026_learning_operations")
        ids = seed(engine)
        asset_id, revision_id, practice_id, item_id = (uuid4() for _ in range(4))
        stream = BytesIO()
        Image.new("RGB", (240, 80), "white").save(stream, format="PNG")
        data = stream.getvalue()
        (profile.storage_root / "content").mkdir(parents=True)
        (profile.storage_root / "content/retained.png").write_bytes(data)
        metadata = registration(data)
        with engine.begin() as c:
            c.execute(text("INSERT INTO question_assets(id,question_id,asset_type,file_path,file_metadata,width,height) VALUES (:a,:q,'figure','content/retained.png',CAST(:m AS jsonb),240,80)"), {"a": asset_id, "q": ids["question"], "m": json.dumps(metadata)})
            c.execute(text("INSERT INTO question_revisions(id,question_id,subject_id,revision,question_type,stem_blocks,options,grounding,admission_state,default_score) VALUES (:r,:q,:s,2,'short_answer',CAST(:b AS jsonb),'[]','general_knowledge','needs_validation',10)"), {"r": revision_id, "q": ids["question"], "s": ids["subject"], "b": json.dumps([{"type": "image", "asset_id": str(asset_id)}])})
            c.execute(text("INSERT INTO question_revision_assets(revision_id,asset_id) VALUES (:r,:a)"), {"r": revision_id, "a": asset_id})
            c.execute(text("INSERT INTO practice_sessions(id,subject_id,state,conditions) VALUES (:p,:s,'preview','{}')"), {"p": practice_id, "s": ids["subject"]})
            c.execute(text("INSERT INTO practice_items(id,session_id,subject_id,position,question_id,question_revision_id,fixed_max_score,scoring_input,knowledge_point_ids) VALUES (:i,:p,:s,1,:q,:r,10,'{}','[]')"), {"i": item_id, "p": practice_id, "s": ids["subject"], "q": ids["question"], "r": revision_id})
            c.execute(text("UPDATE practice_sessions SET state='in_progress' WHERE id=:p"), {"p": practice_id})
        migrate(engine, "head")
        migrate(engine, "head")
        with engine.connect() as c:
            row = c.execute(text("SELECT file_path,file_metadata,source_document_id,source_anchor FROM question_assets WHERE id=:a"), {"a": asset_id}).one()
            assert row[0] == "content/retained.png" and row[1] == metadata
            assert row[2] is None and row[3] is None  # No fabricated provenance.
            assert (profile.storage_root / row[0]).read_bytes() == data
            assert c.scalar(text("SELECT count(*) FROM practice_items")) == 2
            assert c.scalar(text("SELECT count(*) FROM grade_attempts")) == 1
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text("UPDATE question_assets SET file_path='replaced.png' WHERE id=:a"), {"a": asset_id})
