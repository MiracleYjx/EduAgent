"""TCR 005 Benchmark: actual-owner receipts and fail-closed schema leases; Fakes only."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.schemas.settings import (
    DatabaseIdentity,
    OwnedResource,
    ResourceManifest,
)
from backend.app.schemas.settings import TestResourceProfile as ResourceProfile
from scripts import benchmark_corpus as corpus


@pytest.fixture
def fake_benchmark(monkeypatch, tmp_path):
    root = tmp_path / "test-profile"
    root.mkdir()
    profile = ResourceProfile(
        environment="test", environment_id=uuid4(), root=root,
        database_identity=DatabaseIdentity(
            host="test.invalid", database="eduagent_test", user="eduagent_test_runner",
            schema_name="public", instance_id="fake-benchmark-instance",
        ),
    )
    profile_path = tmp_path / "test-resource-profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("EDUAGENT_TEST_RESOURCE_PROFILE", str(profile_path))
    monkeypatch.setenv("EDUAGENT_DATA_ROOT", str(root))
    monkeypatch.setenv("STORAGE_ROOT", str(root / "storage"))
    settings = SimpleNamespace(
        database_url="postgresql+psycopg://eduagent_test_runner@test.invalid:5432/eduagent_test"
    )
    monkeypatch.setattr(corpus, "get_settings", lambda: settings)
    state = SimpleNamespace(
        database=profile.database_identity.database, user=profile.database_identity.user,
        instance=profile.database_identity.instance_id, forced_schema=None,
        schemas={}, writes=[], engines=[], transaction_commits=0,
    )

    class RawConnection:
        path = "public"

        def scalar(self, statement, parameters=()):
            sql = str(statement)
            if "current_database" in sql:
                return state.database
            if "current_user" in sql:
                return state.user
            if "current_schema" in sql:
                return state.forced_schema or (self.path if self.path in state.schemas else "public")
            if "pg_control_system" in sql:
                return state.instance
            if "pg_get_userbyid" in sql:
                name = parameters["name"] if isinstance(parameters, dict) else parameters[0]
                return state.schemas.get(name)
            return 1  # pgvector / SELECT 1 in this Fake only.

        def cursor(self):
            raw = self

            class Cursor:
                value = None

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def execute(self, statement, parameters=()):
                    if str(statement).startswith("SET search_path"):
                        raw.path = str(statement).split('"')[1]
                        self.value = None
                    else:
                        self.value = raw.scalar(statement, parameters)

                def fetchone(self):
                    return (self.value,) if self.value is not None else None

            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        def scalar(self, statement, parameters=None):
            return self.raw.scalar(statement, parameters or ())

        def execute(self, statement):
            state.writes.append((type(statement).__name__, statement.element))
            if isinstance(statement, CreateSchema):
                state.schemas[statement.element] = state.user
            elif isinstance(statement, DropSchema):
                del state.schemas[statement.element]

    class Engine:
        def __init__(self):
            self.listeners = {}
            self.raw = RawConnection()
            self.initialized = False
            self.disposed = False

        def checkout(self):
            if not self.initialized:
                for listener in self.listeners.get("connect", []):
                    listener(self.raw, None)
                self.initialized = True
            for listener in self.listeners.get("checkout", []):
                listener(self.raw, None, None)
            return Connection(self.raw)

        @contextmanager
        def connect(self):
            yield self.checkout()

        @contextmanager
        def begin(self):
            connection = self.checkout()
            before = dict(state.schemas)
            first_write = len(state.writes)
            try:
                yield connection
            except BaseException:
                # Roll back this transaction's DDL, not an out-of-band owner
                # change simulated directly in the server facts by a test.
                for _operation, name in state.writes[first_write:]:
                    if name in before:
                        state.schemas[name] = before[name]
                    else:
                        state.schemas.pop(name, None)
                raise
            else:
                state.transaction_commits += 1

        def dispose(self):
            self.disposed = True

    def create_engine(*args, **kwargs):
        engine = Engine()
        state.engines.append(engine)
        return engine

    def listens_for(engine, name):
        def register(function):
            engine.listeners.setdefault(name, []).append(function)
            return function
        return register

    monkeypatch.setattr(corpus, "create_database_engine", create_engine)
    monkeypatch.setattr(corpus.event, "listens_for", listens_for)
    monkeypatch.setattr(
        corpus.Base.metadata, "create_all",
        lambda *a, **k: pytest.fail("No real metadata or ORM work in this Fake suite"),
    )
    return SimpleNamespace(profile=profile, settings=settings, state=state, root=root)


def _manifest(path, schema):
    path.parent.mkdir(parents=True, exist_ok=True)
    inputs = corpus._input_snapshot("fake-source", [], [], [], {})
    manifest = {
        "manifest_version": 1, "schema": schema,
        "corpus": {"chunks": []}, "queries": [], "annotations": [],
        "inputs": inputs, "input_fingerprint": corpus.fingerprint(inputs),
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _owned_lease(fake, name="corpus"):
    run = uuid4()
    schema = f"test_{run.hex}"
    path = fake.root / "results" / name / "manifest.json"
    _manifest(path, schema)
    owner = fake.profile.database_identity.user
    resource = OwnedResource(kind="schema", target=schema, owner=owner,
                             run_id=run, created_at=datetime.now(UTC))
    record = ResourceManifest(
        environment="test", environment_id=fake.profile.environment_id, run_id=run,
        database_identity=fake.profile.database_identity.model_copy(update={"schema_name": schema}),
        root=corpus._test_receipt_root(fake.profile, schema), resources=(resource,),
    )
    lease = corpus._TestSchemaLease(fake.profile, schema, path, record)
    corpus._persist_test_receipt(lease)
    fake.state.schemas[schema] = owner
    return lease


def _inputs(fake):
    path = fake.root / "inputs"
    path.mkdir()
    (path / "python_basics.md").write_text("Fake text; never embedded.", encoding="utf-8")
    for name in ("chunks", "queries", "annotations"):
        (path / f"{name}.json").write_text("{}" if name == "chunks" else "[]", encoding="utf-8")
    return path


def test_test_receipt_is_exclusive_and_keeps_original_resource_manifest(fake_benchmark):
    fake = fake_benchmark
    lease = _owned_lease(fake)
    path = corpus._test_receipt_path(fake.profile, lease.schema, "creation.json")
    before = path.read_bytes()
    record, original = corpus._load_test_receipt(fake.profile, lease.schema)
    assert record == lease.record
    assert original == lease.manifest_path
    assert record.resources[0].owner == fake.profile.database_identity.user
    assert record.root.is_relative_to(fake.root / "temp")
    with pytest.raises(FileExistsError):
        corpus._persist_test_receipt(lease)
    assert path.read_bytes() == before
    assert fake.state.writes == []


def test_test_schema_without_creation_receipt_is_rejected_before_engine(fake_benchmark):
    fake = fake_benchmark
    schema = f"test_{uuid4().hex}"
    fake.state.schemas[schema] = fake.profile.database_identity.user
    with pytest.raises(ValueError, match="创建收据"):
        corpus.schema_engine(schema)
    assert fake.state.engines == []
    assert fake.state.writes == []


@pytest.mark.parametrize("schema", ["public", "benchmark_" + "a" * 32, "test_other_run"])
def test_test_branch_rejects_prefix_only_or_public_targets(fake_benchmark, schema):
    with pytest.raises(ValueError, match="禁止回退 public"):
        corpus.schema_engine(schema)
    assert fake_benchmark.state.engines == []


@pytest.mark.parametrize("field,value", [
    ("database", "development"), ("user", "developer"),
    ("instance", "another-instance"), ("forced_schema", "public"),
])
def test_actual_target_mismatch_blocks_reads_and_cleanup(fake_benchmark, field, value):
    fake = fake_benchmark
    lease = _owned_lease(fake)
    engine = corpus.schema_engine(lease.schema)
    setattr(fake.state, field, value)
    with (
        pytest.raises(ValueError, match="DATABASE_IDENTITY_MISMATCH"),
        engine.connect(),
    ):
        pytest.fail("wrong actual target cannot be read")
    with pytest.raises(ValueError, match="DATABASE_IDENTITY_MISMATCH"):
        corpus.cleanup_manifest(lease.manifest_path)
    assert lease.schema in fake.state.schemas
    assert fake.state.writes == []


def test_actual_owner_change_blocks_read_and_drop(fake_benchmark):
    fake = fake_benchmark
    lease = _owned_lease(fake)
    engine = corpus.schema_engine(lease.schema)
    fake.state.schemas[lease.schema] = "another-owner"
    with (
        pytest.raises(ValueError, match="SCHEMA_OWNER_CHANGED"),
        engine.connect(),
    ):
        pytest.fail("changed owner cannot be read")
    with pytest.raises(ValueError, match="SCHEMA_OWNER_CHANGED"):
        corpus.cleanup_manifest(lease.manifest_path)
    assert fake.state.schemas[lease.schema] == "another-owner"
    assert fake.state.writes == []


def test_each_checkout_rechecks_missing_receipt(fake_benchmark):
    fake = fake_benchmark
    lease = _owned_lease(fake)
    engine = corpus.schema_engine(lease.schema)
    with engine.connect():
        pass
    corpus._test_receipt_path(fake.profile, lease.schema, "creation.json").unlink()
    with (
        pytest.raises(ValueError, match="创建收据"),
        engine.connect(),
    ):
        pytest.fail("pooled connection cannot outlive its receipt")
    assert fake.state.writes == []


def test_other_run_receipt_cannot_authorize_this_schema(fake_benchmark):
    fake = fake_benchmark
    first = _owned_lease(fake, "first")
    other = _owned_lease(fake, "other")
    first_path = corpus._test_receipt_path(fake.profile, first.schema, "creation.json")
    other_path = corpus._test_receipt_path(fake.profile, other.schema, "creation.json")
    first_path.write_bytes(other_path.read_bytes())  # Synthetic corruption, not a tool write path.
    with pytest.raises(ValueError, match="RECEIPT_MISMATCH"):
        corpus.schema_engine(first.schema)
    assert fake.state.engines == []
    assert fake.state.writes == []


def test_copied_manifest_cannot_cleanup_another_valid_run(fake_benchmark):
    fake = fake_benchmark
    first = _owned_lease(fake, "first")
    other = _owned_lease(fake, "other")
    changed = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    changed["schema"] = other.schema
    first.manifest_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="MANIFEST_NOT_OWNED_BY_RUN"):
        corpus.cleanup_manifest(first.manifest_path)
    assert set(fake.state.schemas) == {first.schema, other.schema}
    assert fake.state.engines == []
    assert fake.state.writes == []


def test_cleanup_uses_t199_guard_and_never_rewrites_creation_receipt(fake_benchmark, monkeypatch):
    fake = fake_benchmark
    lease = _owned_lease(fake)
    path = corpus._test_receipt_path(fake.profile, lease.schema, "creation.json")
    before = path.read_bytes()
    checks = []
    authorize = corpus.authorize_cleanup

    def observed(record, resource, **kwargs):
        checks.append((record, resource, kwargs["actual_database"]))
        authorize(record, resource, **kwargs)

    monkeypatch.setattr(corpus, "authorize_cleanup", observed)
    corpus.cleanup_manifest(lease.manifest_path)
    assert fake.state.writes == [("DropSchema", lease.schema)]
    assert checks and all(item[0] == lease.record for item in checks)
    assert all(item[2] == lease.record.database_identity for item in checks)
    assert path.read_bytes() == before
    assert lease.manifest_path.exists()
    done = corpus._test_receipt_path(fake.profile, lease.schema, "cleanup.json")
    assert ResourceManifest.model_validate_json(done.read_bytes()).cleanup_state == "completed"
    with pytest.raises(ValueError, match="已清理"):
        corpus.schema_engine(lease.schema)
    assert fake.state.writes == [("DropSchema", lease.schema)]


def test_guard_rejection_does_not_fall_back_to_an_unchecked_drop(fake_benchmark, monkeypatch):
    fake = fake_benchmark
    lease = _owned_lease(fake)

    def reject(*args, **kwargs):
        raise ValueError("fake T199 rejection")

    monkeypatch.setattr(corpus, "authorize_cleanup", reject)
    with pytest.raises(ValueError, match="fake T199 rejection"):
        corpus.cleanup_manifest(lease.manifest_path)
    assert lease.schema in fake.state.schemas
    assert fake.state.writes == []


def test_committed_owner_receipt_exists_before_metadata_and_survives_failure(fake_benchmark, monkeypatch):
    fake = fake_benchmark
    inputs = _inputs(fake)
    recorded = []

    def metadata_failed(*args, **kwargs):
        schema = next(iter(fake.state.schemas))
        record, original_path = corpus._load_test_receipt(fake.profile, schema)
        recorded.append((schema, record, original_path))
        assert fake.state.transaction_commits >= 1
        assert record.resources[0].owner == fake.state.schemas[schema]
        raise RuntimeError("fake metadata failed")

    monkeypatch.setattr(corpus.Base.metadata, "create_all", metadata_failed)
    with pytest.raises(RuntimeError, match="fake metadata failed"):
        corpus.setup_corpus(fake.root / "results" / "failed", provider=object(), corpus_dir=inputs)
    schema, record, path = recorded[0]
    assert fake.state.writes == [("CreateSchema", schema), ("DropSchema", schema)]
    assert fake.state.schemas == {}
    assert not path.exists()
    payload = json.loads(corpus._test_receipt_path(fake.profile, schema, "creation.json").read_text(encoding="utf-8"))
    assert ResourceManifest.model_validate(payload["resource_manifest"]) == record
    assert all(engine.disposed for engine in fake.state.engines)


def test_receipt_persistence_failure_keeps_authentic_in_memory_cleanup(fake_benchmark, monkeypatch):
    fake = fake_benchmark
    inputs = _inputs(fake)
    recorded = []

    def failed_persistence(lease):
        recorded.append(lease)
        assert fake.state.transaction_commits >= 1
        assert lease.record.resources[0].owner == fake.state.schemas[lease.schema]
        raise OSError("fake receipt write failure")

    monkeypatch.setattr(corpus, "_persist_test_receipt", failed_persistence)
    with pytest.raises(OSError, match="fake receipt write failure"):
        corpus.setup_corpus(fake.root / "results" / "failed", provider=object(), corpus_dir=inputs)
    schema = recorded[0].schema
    assert fake.state.writes == [("CreateSchema", schema), ("DropSchema", schema)]
    assert fake.state.schemas == {}


def test_setup_failure_preserves_original_error_if_owner_changed(fake_benchmark, monkeypatch):
    fake = fake_benchmark
    inputs = _inputs(fake)

    def failed_metadata(*args, **kwargs):
        schema = next(iter(fake.state.schemas))
        fake.state.schemas[schema] = "another-owner"
        raise RuntimeError("original metadata failure")

    monkeypatch.setattr(corpus.Base.metadata, "create_all", failed_metadata)
    with pytest.raises(RuntimeError, match="original metadata failure") as error:
        corpus.setup_corpus(fake.root / "results" / "failed", provider=object(), corpus_dir=inputs)
    assert [item[0] for item in fake.state.writes] == ["CreateSchema"]
    assert list(fake.state.schemas.values()) == ["another-owner"]
    assert any("cleanup failed" in note and "resource retained" in note for note in error.value.__notes__)


@pytest.mark.parametrize("field,value", [
    ("database", "development"), ("user", "developer"), ("instance", "another-instance"),
])
def test_creation_rejects_wrong_actual_target_before_ddl(fake_benchmark, field, value):
    fake = fake_benchmark
    inputs = _inputs(fake)
    setattr(fake.state, field, value)
    with pytest.raises(ValueError, match="DATABASE_IDENTITY_MISMATCH"):
        corpus.setup_corpus(fake.root / "results" / "forbidden", provider=object(), corpus_dir=inputs)
    assert fake.state.writes == []
    assert not (fake.root / "temp").exists()
    assert fake.state.schemas == {}


def test_receipt_reparse_escape_is_rejected_before_engine(fake_benchmark, monkeypatch):
    fake = fake_benchmark
    lease = _owned_lease(fake)
    creation_path = corpus._test_receipt_path(fake.profile, lease.schema, "creation.json")
    foreign = fake.root.parent / "development-sentinel"
    foreign.write_bytes(b"foreign receipt must not be read or changed")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **k:
                        foreign if path == creation_path else original_resolve(path, *a, **k))
    with pytest.raises(ValueError, match="RECEIPT_PATH_UNVERIFIED"):
        corpus.schema_engine(lease.schema)
    assert fake.state.engines == []
    assert foreign.read_bytes() == b"foreign receipt must not be read or changed"


def test_wrong_configured_url_is_rejected_before_files_or_engine(fake_benchmark):
    fake = fake_benchmark
    fake.settings.database_url = "postgresql+psycopg://developer@dev.invalid:5432/development"
    output = fake.root / "results" / "forbidden"
    with pytest.raises(ValueError, match="TEST_DATABASE_URL_MISMATCH"):
        corpus.setup_corpus(output, provider=object())
    assert not output.exists()
    assert fake.state.engines == []


def test_test_schema_cannot_be_selected_in_non_test_environment(fake_benchmark, monkeypatch):
    lease = _owned_lease(fake_benchmark)
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "development")
    with pytest.raises(ValueError, match="不是 Benchmark"):
        corpus.schema_engine(lease.schema)
    assert fake_benchmark.state.engines == []


def test_non_test_legacy_benchmark_schema_keeps_original_cleanup(fake_benchmark, monkeypatch):
    fake = fake_benchmark
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "development")
    schema = f"benchmark_{uuid4().hex}"
    path = fake.root.parent / "legacy" / "manifest.json"
    original = _manifest(path, schema)
    fake.state.schemas[schema] = "legacy-user"
    corpus.cleanup_manifest(path)
    assert fake.state.writes == [("DropSchema", schema)]
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert not (fake.root / "temp").exists()
    assert set(fake.state.engines[0].listeners) == {"connect"}
