"""T202: synthetic filesystem/SQLite integration, never user data or Providers.

TCR-S1-ENVIRONMENT-PATHS-004. Service assertions use the existing SQL/file facts.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.engine import Engine

from backend.app.core import environment
from backend.app.domain.enums import DocumentStatus
from backend.app.models import Document
from backend.app.schemas.settings import (
    DatabaseLocator,
    EnvironmentName,
    EnvironmentProfile,
    RuntimeKind,
)
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)

owned_document = _owned_document


@pytest.fixture(autouse=True)
def synthetic_boundary(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith(('EDUAGENT_', 'HF_', 'GRADIO_')) or name in {'STORAGE_ROOT', 'SENTENCE_TRANSFORMERS_HOME'}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'user'))
    for name in ('TMP', 'TEMP', 'TMPDIR'):
        monkeypatch.setenv(name, str(tmp_path))
    for name in ('HF_HOME', 'HF_HUB_CACHE', 'SENTENCE_TRANSFORMERS_HOME', 'GRADIO_TEMP_DIR', 'XDG_CACHE_HOME', 'TORCH_HOME'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, 'tempdir', None)
    connect = Engine.connect

    def only_sqlite(engine, *args, **kwargs):
        assert engine.dialect.name == 'sqlite', 'This filesystem suite cannot connect a real database'
        return connect(engine, *args, **kwargs)

    def no_network(*_args, **_kwargs):
        raise AssertionError('No external network or model calls in T202')

    monkeypatch.setattr(Engine, 'connect', only_sqlite)
    monkeypatch.setattr(socket.socket, 'connect', no_network)


def api(name):
    function = getattr(environment, name, None)
    assert callable(function), f'T202 interface not implemented: {name}'
    return function


def profile(root: Path, name: str = 'demo') -> EnvironmentProfile:
    return EnvironmentProfile(
        environment=EnvironmentName(name), environment_id=uuid4(), runtime_kind=RuntimeKind.PYTHON,
        config_root=root / 'config', storage_root=root / 'storage', cache_root=root / 'cache',
        log_root=root / 'logs', temp_root=root / 'temp', results_root=root / 'results',
    )


@pytest.mark.parametrize('name', ['development', 'demo'])
def test_internal_profile_has_all_separate_paths_and_no_creation(tmp_path, name):
    root = tmp_path / name
    resolved = api('resolve_environment_profile')(process_env={
        'EDUAGENT_ENVIRONMENT': name, 'EDUAGENT_DATA_ROOT': str(root),
        'EDUAGENT_ENVIRONMENT_ID': str(uuid4()),
    })
    assert resolved.environment.value == name
    for category in ('config', 'storage', 'cache', 'logs', 'temp', 'results', 'credentials'):
        assert api('environment_path')(resolved, category) == root / category
    assert not root.exists()


def test_test_paths_come_from_explicit_receipt_not_a_development_root(tmp_path):
    root = tmp_path / 'test' / 'runs' / uuid4().hex
    identity = uuid4()
    receipt = tmp_path / 'test-profile.json'
    receipt.write_text(json.dumps({
        'environment': 'test', 'environment_id': str(identity), 'root': str(root),
        'database_identity': {'host': '127.0.0.1', 'port': 15432, 'database': 'eduagent_test',
                              'user': 'eduagent_test_runner', 'schema_name': 'public', 'instance_id': 'synthetic-instance'},
        'redis_instance_id': 'synthetic-redis',
    }), encoding='utf-8')
    source = {'EDUAGENT_ENVIRONMENT': 'test', 'EDUAGENT_TEST_RESOURCE_PROFILE': str(receipt)}
    resolved = api('resolve_environment_profile')(process_env=source)
    assert resolved.environment_id == identity
    assert resolved.storage_root == root / 'storage'
    assert not root.exists()
    with pytest.raises(ValueError):
        api('resolve_environment_profile')(process_env=source | {'EDUAGENT_DATA_ROOT': str(tmp_path / 'development')})


def test_test_missing_profile_never_falls_back_to_development():
    with pytest.raises(ValueError, match='TEST_RESOURCE_PROFILE_REQUIRED'):
        api('resolve_environment_profile')(process_env={'EDUAGENT_ENVIRONMENT': 'test'})


@pytest.mark.parametrize('relative', ['../other', '/outside', 'C:/outside', r'..\other', 'file:stream', 'a//b', 'a/./b'])
def test_controlled_paths_reject_escape_and_noncanonical_inputs(tmp_path, relative):
    with pytest.raises(ValueError):
        api('environment_path')(profile(tmp_path / 'demo'), 'results', relative)
    assert not (tmp_path / 'demo').exists()


def test_profile_rejects_cross_root_and_overlapping_directories(tmp_path):
    original = profile(tmp_path / 'demo').model_dump()
    for change in ({'cache_root': tmp_path / 'development' / 'cache'}, {'log_root': original['storage_root']}):
        with pytest.raises(ValueError):
            EnvironmentProfile.model_validate(original | change)


def test_results_directory_cannot_override_another_environment(tmp_path):
    selected = profile(tmp_path / 'test', 'test')
    assert api('resolve_results_directory')(None, profile=selected) == selected.results_root
    assert api('resolve_results_directory')(selected.results_root / 'run-a', profile=selected) == selected.results_root / 'run-a'
    with pytest.raises(ValueError):
        api('resolve_results_directory')(tmp_path / 'development' / 'results', profile=selected)


def test_old_resource_preview_never_moves_or_deletes_bytes(tmp_path):
    old = tmp_path / 'old-storage'
    old.mkdir()
    sentinel = old / 'original.txt'
    sentinel.write_bytes(b'original development material')
    locator = DatabaseLocator(host='database.invalid', database='legacy', user='legacy')
    proposed = profile(tmp_path / 'development', 'development').model_copy(update={'database_locator': locator})
    view = api('preview_legacy_resources')(legacy_database=locator, legacy_storage_root=old, proposed_profile=proposed)
    assert view['requires_confirmation'] is True
    assert 'LEGACY_DATABASE_STORAGE_MISMATCH' in view['conflicts']
    assert view['moves_files'] is False and view['deletes_files'] is False
    assert sentinel.read_bytes() == b'original development material'
    assert not proposed.storage_root.exists()


def test_old_database_cannot_write_new_empty_storage(owned_document):
    session, doc, teacher, old = owned_document
    old_service = FileStorageService(session, root=old)
    stored = old_service.store_document(doc, b'original bytes', actor_id=teacher.id)
    doc.storage_path = stored.storage_path
    doc.file_metadata = stored.metadata.model_dump(mode='json')
    session.commit()
    old_service.commit_receipt(stored, current_status=doc.status.value)
    candidate = Document(id=uuid4(), course_id=doc.course_id, knowledge_base_id=doc.knowledge_base_id,
                         uploaded_by=teacher.id, original_filename='new.txt', file_format='txt', status=DocumentStatus.UPLOADED)
    new = old.parent / 'new-empty-storage'
    with pytest.raises(FileStorageError) as error:
        FileStorageService(session, root=new).store_document(candidate, b'new bytes', actor_id=teacher.id)
    assert error.value.code == 'FILE_STORAGE_ADOPTION_REQUIRED'
    assert not new.exists()
    assert old_service.read_document(doc) == b'original bytes'


def test_fresh_profile_storage_is_written_and_read_without_touching_sentinels(owned_document):
    session, doc, teacher, old = owned_document
    demo = profile(old.parent / 'demo')
    development = old.parent / 'development'
    development.mkdir()
    sentinel = development / 'sentinel.bin'
    sentinel.write_bytes(b'non-test sentinel')
    service = FileStorageService(session, profile=demo)
    stored = service.store_document(doc, b'owned new bytes', actor_id=teacher.id)
    doc.storage_path = stored.storage_path
    doc.file_metadata = stored.metadata.model_dump(mode='json')
    session.commit()
    service.commit_receipt(stored, current_status=doc.status.value)
    assert service.root == demo.storage_root
    assert service.read_document(doc) == b'owned new bytes'
    assert sentinel.read_bytes() == b'non-test sentinel'
    assert service.resolve_path(stored.storage_path).is_relative_to(demo.storage_root)


def test_non_development_profile_cannot_read_foreign_legacy_bytes(owned_document):
    session, doc, _teacher, old = owned_document
    old.mkdir()
    foreign = old / 'private.txt'
    foreign.write_bytes(b'development-private')
    doc.storage_path = str(foreign)
    with pytest.raises(FileStorageError):
        FileStorageService(session, profile=profile(old.parent / 'demo')).read_document(doc)
    assert foreign.read_bytes() == b'development-private'


def test_runtime_cache_temp_logs_are_env_owned_without_provider_loading(tmp_path, monkeypatch):
    selected = profile(tmp_path / 'demo')
    for name in ('HF_HOME', 'HF_HUB_CACHE', 'GRADIO_TEMP_DIR', 'SENTENCE_TRANSFORMERS_HOME'):
        monkeypatch.delenv(name, raising=False)
    api('prepare_runtime_paths')(selected)
    assert Path(tempfile.gettempdir()) == selected.temp_root
    assert Path(os.environ['HF_HOME']).is_relative_to(selected.cache_root)
    assert Path(os.environ['GRADIO_TEMP_DIR']).is_relative_to(selected.temp_root)
    assert selected.log_root.is_dir()
    assert not selected.storage_root.exists()  # preparation is not storage adoption


def test_evaluation_defaults_to_selected_results_but_keeps_authorization(tmp_path):
    from backend.app.services.evaluation_service import EvaluationService
    selected = profile(tmp_path / 'demo')
    selected.results_root.mkdir(parents=True)
    (selected.results_root / 'retrieval_case_hybrid.json').write_text(json.dumps({
        'run_id': 'case', 'config': 'hybrid', 'status': 'ok', 'results': [],
    }), encoding='utf-8')
    actor = uuid4()
    service = EvaluationService(profile=selected, authorize=lambda who: who == actor)
    assert service.query(actor_id=uuid4()) == []
    assert service.query(actor_id=actor)[0].run_id == 'case'
    assert EvaluationService(profile=selected).query(actor_id=actor) == []


def test_all_three_environment_file_sentinels_survive_test_output(tmp_path):
    profiles = [profile(tmp_path / name, name) for name in ('development', 'test', 'demo')]
    sentinels = {}
    for selected in profiles:
        selected.storage_root.mkdir(parents=True)
        path = selected.storage_root / 'sentinel'
        path.write_bytes(selected.environment.value.encode())
        sentinels[path] = path.read_bytes()
    target = api('environment_path')(profiles[1], 'results', 'run-a/record.json')
    target.parent.mkdir(parents=True)
    target.write_text('{"synthetic":true}', encoding='utf-8')
    assert all(path.read_bytes() == data for path, data in sentinels.items())
    assert not profiles[0].results_root.exists() and not profiles[2].results_root.exists()


def test_results_writers_use_selected_profile_without_changing_record_formats(tmp_path, monkeypatch):
    from scripts import generate_synthetic_benchmark, run_grading_benchmark
    selected = profile(tmp_path / 'demo')
    monkeypatch.setenv('EDUAGENT_ENVIRONMENT', 'demo')
    monkeypatch.setenv('EDUAGENT_DATA_ROOT', str(tmp_path / 'demo'))
    monkeypatch.setenv('EDUAGENT_ENVIRONMENT_ID', str(selected.environment_id))
    generated = generate_synthetic_benchmark.generate_synthetic_benchmark(count=10)
    json_path, _csv = generate_synthetic_benchmark.write_benchmark_results(generated)
    assert json_path.is_relative_to(selected.results_root)
    record = {'run_id': 'stub', 'created_at': '2026-10-15T00:00:00+00:00', 'mode': 'selftest',
              'dataset': {'version': 'synthetic'}, 'runs': [], 'ground_truth': {}}
    graded, _summary = run_grading_benchmark.write_run_records(record, run_id='stub')
    assert graded.is_relative_to(selected.results_root)
    assert json.loads(graded.read_text()) == record


@pytest.mark.parametrize('run_id', ['../private', 'a/b', r'a\b', 'file:stream'])
def test_benchmark_run_identity_cannot_escape_results(tmp_path, run_id):
    from scripts.run_grading_benchmark import write_run_records
    with pytest.raises(ValueError):
        write_run_records({'runs': []}, results_dir=tmp_path / 'results', run_id=run_id)
    assert not (tmp_path / 'results').exists()


@pytest.mark.parametrize('category', ['config', 'storage', 'cache', 'logs', 'temp', 'results', 'credentials'])
def test_late_reparse_points_cannot_redirect_environment_operations(tmp_path, category):
    selected = profile(tmp_path / 'demo')
    selected.config_root.parent.mkdir()
    foreign = tmp_path / 'development-private'
    foreign.mkdir()
    sentinel = foreign / 'sentinel'
    sentinel.write_bytes(b'foreign bytes must survive')
    link = selected.config_root.parent / category
    try:
        if os.name == 'nt':
            import _winapi
            create_junction = getattr(_winapi, 'CreateJunction', None)
            if create_junction is None:
                pytest.skip('Installed Python does not expose Windows junction creation')
            create_junction(str(foreign), str(link))
        else:
            link.symlink_to(foreign, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'Synthetic reparse fixture unavailable: {exc}')
    with pytest.raises(ValueError):
        environment.environment_path(selected, category, 'new-file')
    with pytest.raises(ValueError):
        EnvironmentProfile.model_validate(selected.model_dump())
    assert sentinel.read_bytes() == b'foreign bytes must survive'
    assert not (foreign / 'new-file').exists()


def test_copied_profile_cannot_bypass_the_operation_boundary(tmp_path):
    selected = profile(tmp_path / 'demo')
    forged = selected.model_copy(update={'cache_root': tmp_path / 'development' / 'cache'})
    with pytest.raises(ValueError):
        environment.environment_path(forged, 'cache')
    assert not selected.config_root.parent.exists()


def test_explicit_settings_storage_cannot_escape_selected_demo(tmp_path, monkeypatch):
    from tests.unit.settings_helpers import build_test_settings
    selected = profile(tmp_path / 'demo')
    monkeypatch.setenv('EDUAGENT_ENVIRONMENT', 'demo')
    monkeypatch.setenv('EDUAGENT_DATA_ROOT', str(selected.config_root.parent))
    assert build_test_settings(STORAGE_ROOT=selected.storage_root).storage_root == selected.storage_root
    with pytest.raises(ValueError):
        build_test_settings(STORAGE_ROOT=tmp_path / 'development' / 'storage')
    monkeypatch.setenv('EDUAGENT_ENVIRONMENT', 'test')
    with pytest.raises(ValueError):
        build_test_settings(STORAGE_ROOT=selected.storage_root)
    assert not selected.config_root.parent.exists()


def test_source_entry_delegates_to_create_application(tmp_path, monkeypatch):
    """T218：源码入口只委派 `create_application()`，不直接导入业务工厂。"""

    import runpy
    import sys
    from types import ModuleType

    from fastapi import FastAPI

    selected = profile(tmp_path / 'demo')
    monkeypatch.setenv('EDUAGENT_ENVIRONMENT', 'demo')
    monkeypatch.setenv('EDUAGENT_DATA_ROOT', str(selected.config_root.parent))
    calls = []
    bootstrap_module = ModuleType('backend.app.core.bootstrap')
    application = FastAPI()

    def create_application(**_kwargs):
        calls.append('application')
        return application

    bootstrap_module.create_application = create_application
    bootstrap_module.listen_address = lambda: ('127.0.0.1', 8000)
    app_module = ModuleType('backend.app.core.app')
    app_module.create_app = lambda **_kwargs: pytest.fail('业务工厂不得由 backend.main 直接导入')
    monkeypatch.setitem(sys.modules, 'backend.app.core.bootstrap', bootstrap_module)
    monkeypatch.setitem(sys.modules, 'backend.app.core.app', app_module)

    namespace = runpy.run_module('backend.main', run_name='t202_source_entry')

    assert namespace['app'] is application and calls == ['application']


def test_windows_entry_uses_environment_logs_before_setup_worker(tmp_path, monkeypatch):

    from backend.app.deployment import windows_launcher as launcher
    selected = profile(tmp_path / 'demo')
    monkeypatch.setenv('EDUAGENT_ENVIRONMENT', 'demo')
    monkeypatch.setenv('EDUAGENT_DATA_ROOT', str(selected.config_root.parent))
    monkeypatch.setattr(launcher, 'port_available', lambda *_a, **_k: True)
    calls = []

    def stop_at_worker(**_kwargs):
        assert Path(tempfile.gettempdir()) == selected.temp_root
        assert Path(os.environ['GRADIO_TEMP_DIR']).is_relative_to(selected.temp_root)
        assert (selected.log_root / 'application.log').is_file()
        assert not (selected.storage_root / 'logs').exists()
        calls.append('setup-worker')
        raise launcher.LaunchError('synthetic stop', 'no model or DB call', 'T202 fixture')

    monkeypatch.setattr(launcher, '_spawn_worker', stop_at_worker)
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('No worker may start'))
    assert launcher.main(['--config', str(tmp_path / 'synthetic.env'), '--no-browser']) == 1
    assert calls == ['setup-worker']


def test_retrieval_writer_uses_selected_profile_and_retains_record_format(tmp_path):
    from scripts import run_retrieval_benchmark as benchmark
    selected = profile(tmp_path / 'demo')
    run = benchmark.ConfigRun(config=benchmark.RetrievalMode.KEYWORD_ONLY, per_query=[], metrics={},
                              status='failed', error_code='SYNTHETIC_FAILED', error_message='safe failure')
    saved = benchmark.write_run_records(run, run_id='case', profile=selected, dataset_version='synthetic',
                                       model_version='stub', prompt_version=None, top_k=5, analysis='selftest')
    assert saved.is_relative_to(selected.results_root)
    record = json.loads(saved.read_text(encoding='utf-8'))
    assert record['status'] == 'failed' and record['metrics'] == {} and record['results'] == []
    assert record['error_code'] == 'SYNTHETIC_FAILED' and record['run_id'] == 'case'
    assert (selected.results_root / benchmark.SUMMARY_NAME).is_file()


@pytest.mark.parametrize('manifest_mode', [False, True])
@pytest.mark.parametrize('run_id', ['../escape', 'a/b', r'a\b', 'file:stream', ''])
def test_retrieval_entry_rejects_unsafe_ids_before_inputs_or_side_effects(tmp_path, monkeypatch, manifest_mode, run_id):
    from scripts import run_retrieval_benchmark as benchmark
    monkeypatch.setattr(benchmark, 'load_dataset', lambda: pytest.fail('Must validate before reading input'))
    monkeypatch.setattr(benchmark, 'get_settings', lambda: pytest.fail('Must validate before settings/DB/Provider'))
    args = ['--self-test', '--run-id', run_id, '--output-dir', str(tmp_path / 'results')]
    if manifest_mode:
        args += ['--manifest', str(tmp_path / 'not-read.json')]
    assert benchmark.main(args) == 1
    assert not (tmp_path / 'results').exists()


def test_manifest_failure_and_no_overwrite_stay_in_selected_results(tmp_path, monkeypatch):
    from scripts import benchmark_corpus
    from scripts import run_retrieval_benchmark as benchmark
    selected = profile(tmp_path / 'demo')
    monkeypatch.setenv('EDUAGENT_ENVIRONMENT', 'demo')
    monkeypatch.setenv('EDUAGENT_DATA_ROOT', str(selected.config_root.parent))
    reads = []

    def fail_input(_path):
        reads.append('read')
        raise ValueError('synthetic invalid manifest')

    monkeypatch.setattr(benchmark_corpus, 'load_manifest', fail_input)
    monkeypatch.setattr(benchmark_corpus, 'schema_engine', lambda _: pytest.fail('No DB access'))
    args = ['--self-test', '--manifest', str(tmp_path / 'fixture.json'), '--run-id', 'case']
    assert benchmark.main(args) == 1
    failure = selected.results_root / 'case' / 'failure.json'
    original = failure.read_bytes()
    assert json.loads(original)['status'] == 'failed'
    assert benchmark.main(args) == 1
    assert failure.read_bytes() == original and reads == ['read']


@pytest.mark.parametrize('writer', ['retrieval', 'grading', 'synthetic'])
@pytest.mark.parametrize('target', ['json', 'summary'])
def test_result_writers_reject_reparse_targets_before_any_output(tmp_path, writer, target):
    from scripts import generate_synthetic_benchmark as synthetic
    from scripts import run_grading_benchmark as grading
    from scripts import run_retrieval_benchmark as retrieval

    selected = profile(tmp_path / 'test', 'test')
    if writer == 'retrieval':
        names = ('retrieval_case_keyword_only.json', retrieval.SUMMARY_NAME)
        run = retrieval.ConfigRun(config=retrieval.RetrievalMode.KEYWORD_ONLY, per_query=[], metrics={},
                                  status='failed', error_code='SYNTHETIC_FAILED', error_message='safe failure')

        def write():
            retrieval.write_run_records(run, run_id='case', profile=selected, dataset_version='synthetic',
                                        model_version='stub', prompt_version=None, top_k=5, analysis='selftest')
    elif writer == 'grading':
        names = ('grading_case.json', grading.SUMMARY_FILENAME)

        def write():
            grading.write_run_records({'runs': []}, run_id='case', profile=selected)
    else:
        names = (synthetic.DEFAULT_JSON_NAME, synthetic.DEFAULT_CSV_NAME)
        dataset = synthetic.generate_synthetic_benchmark(count=10)

        def write():
            synthetic.write_benchmark_results(dataset, profile=selected)

    selected.results_root.mkdir(parents=True)
    foreign = tmp_path / 'development' / 'results'
    foreign.mkdir(parents=True)
    sentinel = foreign / 'sentinel.bin'
    sentinel.write_bytes(b'foreign results must remain unchanged')
    link = selected.results_root / names[0 if target == 'json' else 1]
    other = selected.results_root / names[1 if target == 'json' else 0]
    other.write_bytes(b'previous result must remain unchanged')
    try:
        if os.name == 'nt':
            import _winapi
            create_junction = getattr(_winapi, 'CreateJunction', None)
            if create_junction is None:
                pytest.skip('Installed Python does not expose Windows junction creation')
            create_junction(str(foreign), str(link))
        else:
            link.symlink_to(foreign, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'Synthetic reparse fixture unavailable: {exc}')

    with pytest.raises(environment.EnvironmentSelectionError, match='ENVIRONMENT_PATH_UNVERIFIED'):
        write()
    assert sentinel.read_bytes() == b'foreign results must remain unchanged'
    assert list(foreign.iterdir()) == [sentinel]
    assert other.read_bytes() == b'previous result must remain unchanged'
