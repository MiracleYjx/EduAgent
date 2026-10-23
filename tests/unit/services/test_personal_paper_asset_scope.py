"""Offline P1 scope/retry regressions; no persisted operations or provider calls.

TCR: docs/test-change-record-personal-learning.md, P1 OCR asset scope section.
Concurrency doubles verify service serialization, not PostgreSQL lock behavior.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier, RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.ai.paper_extraction.schemas import PersonalPaperCandidate
from backend.app.schemas.content_blocks import SourceLocation
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.paper_import_service import PersonalPaperImportService


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    import socket

    from sqlalchemy.engine import Engine

    def forbidden(*args, **kwargs):
        raise AssertionError("P1 unit tests forbid network and database connections")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)


def scope_case():
    document = SimpleNamespace(id=uuid4(), subject_id=uuid4(), parse_facts={
        "blocks": [
            {"type": "image", "anchor": "page-1", "page": 1,
             "sha256": "a" * 64, "media_type": "image/png"},
            {"type": "text", "anchor": "page-1:ocr", "page": 1,
             "text": "Original question", "ocr_source_anchor": "page-1"},
            {"type": "image", "anchor": "page-1-other", "page": 1,
             "sha256": "b" * 64, "media_type": "image/png"},
            {"type": "image", "anchor": "page-2", "page": 2,
             "sha256": "c" * 64, "media_type": "image/png"},
        ],
        "assets": [{"sha256": char * 64, "stored": True} for char in "abc"],
    })
    service = object.__new__(PersonalPaperImportService)
    service.materials = SimpleNamespace(asset_path=lambda *args: None)
    catalog = service._catalog(document)
    asset = next(a for a in catalog.values() if a.source.anchor == "page-1")
    candidate = PersonalPaperCandidate(
        question_type="short_answer",
        stem_blocks=[{"type": "image", "asset_id": asset.id}],
        source_locations=[SourceLocation(document_id=document.id, page=1, anchor="page-1:ocr")],
    )
    return service, document, catalog, candidate


def test_persisted_ocr_link_accepts_allowed_image_without_mutation():
    service, document, catalog, candidate = scope_case()
    before = deepcopy(document.parse_facts)
    service._validate_sources(candidate, document, catalog)
    assert document.parse_facts == before
    assert service._catalog(document) == catalog


def test_direct_image_source_remains_valid():
    service, document, catalog, candidate = scope_case()
    candidate.source_locations[0].anchor = "page-1"
    service._validate_sources(candidate, document, catalog)


@pytest.mark.parametrize("case", ["unknown", "foreign", "other_same_page", "wrong_page",
                                 "missing_link", "wrong_link", "non_text_link"])
def test_ocr_link_never_expands_asset_scope(case):
    service, document, catalog, candidate = scope_case()
    if case == "unknown":
        candidate.stem_blocks[0].asset_id = uuid4()
    elif case == "foreign":
        _, _, foreign, other = scope_case()
        catalog.update(foreign)
        candidate.stem_blocks[0].asset_id = other.stem_blocks[0].asset_id
    elif case in {"other_same_page", "wrong_page"}:
        anchor = "page-1-other" if case == "other_same_page" else "page-2"
        candidate.stem_blocks[0].asset_id = next(a.id for a in catalog.values()
                                                if a.source.anchor == anchor)
    elif case == "missing_link":
        document.parse_facts["blocks"][1].pop("ocr_source_anchor")
    elif case == "wrong_link":
        document.parse_facts["blocks"][1]["ocr_source_anchor"] = "unrelated"
    else:
        document.parse_facts["blocks"][1]["type"] = "image"
    with pytest.raises(MaterialError, match="PAPER_ASSET_SCOPE_INVALID"):
        service._validate_sources(candidate, document, catalog)


def test_foreign_document_source_still_rejected():
    service, document, catalog, candidate = scope_case()
    candidate.source_locations[0].document_id = uuid4()
    with pytest.raises(MaterialError, match="PAPER_SOURCE_SCOPE_INVALID"):
        service._validate_sources(candidate, document, catalog)


def test_original_block_requires_exact_image_source():
    service, document, catalog, candidate = scope_case()
    candidate = PersonalPaperCandidate(
        question_type="short_answer",
        stem_blocks=[{"type": "original", "asset_id": candidate.stem_blocks[0].asset_id,
                      "source": candidate.source_locations[0], "reason": "review"}],
        source_locations=candidate.source_locations,
    )
    with pytest.raises(MaterialError, match="PAPER_ASSET_SOURCE_INVALID"):
        service._validate_sources(candidate, document, catalog)


class MemorySessions:
    """Serialize the import transaction without any database or OperationRun."""

    def __init__(self, records):
        self.records = records
        self.lock = RLock()

    @contextmanager
    def begin(self):
        with self.lock:
            yield self

    def get(self, model, identity):
        return self.records.get(identity)


def retry_case():
    service, document, catalog, candidate = scope_case()
    old = SimpleNamespace(id=uuid4(), retry_of_id=None, state="failed", actual_requests=1,
                          idempotency_key="old", runtime_snapshot_id=uuid4(),
                          steps=[{"messages": [{"document_id": str(document.id),
                                   "assets": [a.model_dump(mode="json") for a in catalog.values()]}],
                                  "error_code": "ProviderEmptyResponse"}])
    paper = SimpleNamespace(id=uuid4(), document_id=document.id,
                            extraction_operation_id=old.id)
    records = {old.id: old}
    sessions = MemorySessions(records)
    service.sessions = sessions
    locks = []

    def record(session, identity, *, lock=False):
        assert identity == paper.id
        locks.append(lock)
        return paper

    service._record = record
    calls = []

    def retry(identity, *, idempotency_key, acknowledge_unknown):
        assert identity == old.id
        calls.append(identity)
        operation = SimpleNamespace(id=uuid4(), retry_of_id=old.id, state="prepared",
                                    actual_requests=0, idempotency_key=idempotency_key,
                                    runtime_snapshot_id=uuid4(), steps=deepcopy(old.steps))
        records[operation.id] = operation
        return operation

    operations = SimpleNamespace(retry=retry, view=lambda identity: records[identity])
    return service, document, catalog, candidate, paper, old, operations, calls, locks


def test_retry_inherits_import_scope_and_keeps_failed_audit():
    service, document, catalog, candidate, paper, old, operations, calls, locks = retry_case()
    before = deepcopy(vars(old))
    original_scope = deepcopy(document.parse_facts)
    retry = service.prepare_extraction(paper.id, operations, "retry", retry_of_id=old.id)
    assert retry.retry_of_id == old.id
    assert paper.extraction_operation_id == retry.id
    assert retry.steps == old.steps
    assert vars(old) == before
    assert operations.view(old.id).state == "failed"
    assert operations.view(old.id).actual_requests == 1
    assert document.parse_facts == original_scope
    assert service._catalog(document) == catalog
    service._validate_sources(candidate, document, catalog)
    again = service.prepare_extraction(paper.id, operations, "retry", retry_of_id=old.id)
    assert again.id == retry.id and len(calls) == 1
    assert locks == [True, True]
    with pytest.raises(MaterialError, match="PAPER_OPERATION_CHANGED"):
        service.prepare_extraction(paper.id, operations, "different", retry_of_id=old.id)
    assert len(calls) == 1 and vars(old) == before


def test_concurrent_duplicate_retry_returns_one_transition():
    service, _, _, _, paper, old, operations, calls, locks = retry_case()
    before = deepcopy(vars(old))
    barrier = Barrier(2)

    def prepare():
        barrier.wait(timeout=5)
        return service.prepare_extraction(paper.id, operations, "same", retry_of_id=old.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(prepare) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert results[0].id == results[1].id == paper.extraction_operation_id
    assert len(calls) == 1 and locks == [True, True]
    assert vars(old) == before


def test_production_retry_copies_source_plan_and_links_old_operation():
    from backend.app.services.learning_operations import LearningOperations, StepPlan

    step = StepPlan(step_id="paper", task_kind="paper_extraction", request_budget=1,
                    messages=[{"role": "user", "content": "immutable paper assets"}])
    old = SimpleNamespace(id=uuid4(), purpose="paper_extraction:synthetic",
                          state="failed", actual_requests=1,
                          steps=[{**step.model_dump(mode="json"), "state": "failed",
                                  "error_code": "ProviderEmptyResponse", "result": None}])
    before = deepcopy(vars(old))
    prepared = []

    def prepare(**kwargs):
        prepared.append(kwargs)
        return kwargs

    operations = SimpleNamespace(view=lambda identity: old, prepare=prepare)
    result = LearningOperations.retry(operations, old.id, idempotency_key="explicit")
    assert result["retry_of_id"] == old.id
    assert result["purpose"] == old.purpose
    assert result["steps"] == [step]
    assert result["steps"][0].messages == old.steps[0]["messages"]
    assert vars(old) == before and len(prepared) == 1


def test_concurrent_different_retry_keys_allow_only_one_transition():
    service, _, _, _, paper, old, operations, calls, locks = retry_case()
    barrier = Barrier(2)

    def prepare(key):
        barrier.wait(timeout=5)
        try:
            return service.prepare_extraction(paper.id, operations, key, retry_of_id=old.id)
        except MaterialError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(prepare, key) for key in ("first", "second")]
        results = [future.result(timeout=5) for future in futures]
    accepted = [value for value in results if not isinstance(value, str)]
    rejected = [value for value in results if isinstance(value, str)]
    assert len(accepted) == len(rejected) == 1
    assert accepted[0].id == paper.extraction_operation_id
    assert "PAPER_OPERATION_CHANGED" in rejected[0]
    assert len(calls) == 1 and locks == [True, True]
