"""TCR-012: explicit legacy selection, privacy, conflict and failure evidence."""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import SecretStr

from backend.app.deployment import configuration_editor as editor
from backend.app.services.settings.config_service import ConfigService
from tests.support.credential_fakes import (
    FakeFileSecurity,
    FakeProtector,
    assert_no_secret,
)


def importer(service, sources):
    cls = getattr(editor, 'LegacyConfigImporter', None)
    assert callable(cls), 'T210_IMPLEMENTATION_REQUIRED: LegacyConfigImporter'
    return cls(service=service, sources=sources)


def vault(service):
    method = getattr(service, 'open_credentials', None)
    assert callable(method), 'T210_IMPLEMENTATION_REQUIRED: open_credentials'
    return method(backend='dpapi', protector=FakeProtector(), file_security=FakeFileSecurity())


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'selected-legacy.env'
    secrets = {'database': 't210_db_' + uuid4().hex, 'redis': 't210_redis_' + uuid4().hex,
               'jwt': 't210_signing_' + uuid4().hex, 'provider': 't210_key_${UNEXPANDED}_' + uuid4().hex}
    text = ('# operator-selected legacy source; leave byte-for-byte\n'
        f'DATABASE_URL=postgresql+psycopg://runner:{secrets["database"]}@database.invalid:5432/old_test\n'
        f'REDIS_URL=redis://:{secrets["redis"]}@redis.invalid:6379/2\n'
        f'JWT_SECRET_KEY={secrets["jwt"]}\n'
        'LLM_PROVIDER=openai_compatible\nDEEPSEEK_MODEL=synthetic-model\n'
        'DEEPSEEK_BASE_URL=https://model.invalid/v1\n'
        f'DEEPSEEK_API_KEY=\'{secrets["provider"]}\'\n'
        f'STORAGE_ROOT={tmp_path / "legacy-storage"}\n'
        'RERANK_PROVIDER=none\nOCR_ENABLED=false\nCONFIDENCE_THRESHOLD=0.71\n')
    path.write_text(text, encoding='utf-8')
    return path, secrets


@pytest.fixture
def service(tmp_path):
    with ConfigService(tmp_path / 'environment', uuid4()) as selected:
        yield selected


def public_files(service):
    return {p.relative_to(service.root).as_posix(): p.read_bytes()
            for p in service.config.rglob('*.json')}


def test_preview_is_explicit_read_only_and_never_exposes_secrets(service, source, monkeypatch):
    path, markers = source
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'unrelated_ambient_secret')
    before = path.read_bytes()
    selected = importer(service, {'selected': path})
    preview = selected.preview(source_id='selected', expected_revision=None)
    assert preview.source_id == 'selected'
    assert 'LEGACY_PLAINTEXT_REMAINS' in preview.warnings
    assert 'PROCESS_ENV_OVERRIDES_SAVED' in preview.warnings
    assert repository_empty(service)
    assert not (service.root / 'credentials').exists()
    assert path.read_bytes() == before
    assert_no_secret(markers, {'preview': preview.model_dump_json(), 'repr': repr(preview), **public_files(service)})


def repository_empty(service):
    return service.read_manifest().active_revision is None and not list((service.config / 'drafts').glob('*.json'))


def test_confirmation_writes_only_safe_draft_and_leaves_legacy_source(service, source):
    path, markers = source
    store = vault(service)
    selected = importer(service, {'selected': path})
    preview = selected.preview(source_id='selected', expected_revision=None)
    before = path.read_bytes()
    result = selected.apply(preview_id=preview.preview_id, confirm=True)
    assert result.status == 'draft'
    assert service.read_manifest().active_revision is None
    assert service.read_manifest().pending_restart_revision is None
    assert path.read_bytes() == before
    assert result.values.business.database_locator.database == 'old_test'
    assert result.values.business.redis_locator.database == 2
    assert result.values.business.storage_root == path.parent / 'legacy-storage'
    refs = [result.values.business.database_password_ref, result.values.business.redis_password_ref,
            result.values.business.jwt_secret_ref, next(iter(result.values.providers.values())).credential_ref]
    assert {ref.purpose for ref in refs} == {'database', 'redis', 'jwt', 'provider'}
    for ref in refs:
        actual = store.get(ref, ref.version)
        if actual != SecretStr(markers[ref.purpose]):
            pytest.fail('synthetic imported credential mismatch', pytrace=False)
    assert not result.activation_validation
    assert_no_secret(markers, {'result': result.model_dump_json(), **public_files(service)})
    with pytest.raises(ValueError, match='PREVIEW_NOT_FOUND'):
        selected.apply(preview_id=preview.preview_id, confirm=True)


@pytest.mark.parametrize('confirm', [False, None, 1, 'true'])
def test_apply_requires_literal_confirmation(service, source, confirm):
    path, _ = source
    vault(service)
    selected = importer(service, {'selected': path})
    preview = selected.preview(source_id='selected', expected_revision=None)
    with pytest.raises(ValueError, match='IMPORT_CONFIRMATION_REQUIRED'):
        selected.apply(preview_id=preview.preview_id, confirm=confirm)
    assert repository_empty(service)


@pytest.mark.parametrize('source_id', ['missing', '../selected', 'https://foreign.invalid/env', '/arbitrary/.env'])
def test_unknown_source_ids_never_open_arbitrary_files(service, source, source_id):
    path, _ = source
    selected = importer(service, {'selected': path})
    with pytest.raises(ValueError, match='LEGACY_SOURCE_NOT_ALLOWED'):
        selected.preview(source_id=source_id, expected_revision=None)
    assert repository_empty(service)


def test_source_change_requires_new_preview_and_does_not_write_secrets(service, source):
    path, _ = source
    store = vault(service)
    selected = importer(service, {'selected': path})
    preview = selected.preview(source_id='selected', expected_revision=None)
    path.write_bytes(path.read_bytes() + b'# external edit\n')
    with pytest.raises(ValueError, match='LEGACY_SOURCE_CHANGED'):
        selected.apply(preview_id=preview.preview_id, confirm=True)
    assert not list(store.credentials.glob('*.dpapi'))
    assert repository_empty(service)


def test_revision_conflict_is_checked_before_importing_credentials(service, source):
    from tests.support.credential_fakes import baseline_values
    path, _ = source
    store = vault(service)
    selected = importer(service, {'selected': path})
    preview = selected.preview(source_id='selected', expected_revision=None)
    ref = store.put(purpose='jwt', secret_value=SecretStr('t210_preexisting_' + uuid4().hex))
    draft = service.create_draft(baseline_values(service.root, service.environment_id, ref), expected_revision=None, process_env={})
    service.validate(draft.revision_id, expected_revision=None, process_env={})
    active = service.activate(draft.revision_id, expected_revision=None, process_env={})
    before = (service.config / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        selected.apply(preview_id=preview.preview_id, confirm=True)
    assert (service.config / 'manifest.json').read_bytes() == before
    assert len(list(store.credentials.glob('*.dpapi'))) == 1
    assert service.active_revision().revision_id == active.revision_id


@pytest.mark.parametrize('failure', ['no_backend', 'protect', 'draft_write'])
def test_import_failure_never_changes_old_source_or_active(service, source, monkeypatch, failure):
    path, markers = source
    before = path.read_bytes()
    if failure != 'no_backend':
        store = vault(service)
        if failure == 'protect':
            store._protector.protect_error = True
        else:
            original = service._atomic_write
            def fail(target, payload, stage):
                if stage == 'draft':
                    raise OSError('t210_synthetic_sensitive_failure')
                return original(target, payload, stage)
            monkeypatch.setattr(service, '_atomic_write', fail)
    selected = importer(service, {'selected': path})
    preview = selected.preview(source_id='selected', expected_revision=None)
    with pytest.raises((ValueError, RuntimeError)) as error:
        selected.apply(preview_id=preview.preview_id, confirm=True)
    assert path.read_bytes() == before
    assert service.read_manifest().active_revision is None
    assert repository_empty(service)
    assert_no_secret(markers, {'error': str(error.value), **public_files(service)})


@pytest.mark.parametrize('line', ['JWT_SECRET_KEY=', 'DATABASE_URL=not-a-url',
    'DEEPSEEK_BASE_URL=https://key:secret@model.invalid/v1', 'EMBEDDING_DIMENSION=768',
    'OCR_ENABLED=not-a-bool', 'UNKNOWN_SECRET=never-import-this', "BAD='unterminated"])
def test_invalid_legacy_input_is_not_silently_partially_imported(service, source, line):
    path, _ = source
    path.write_bytes(path.read_bytes() + (line+'\n').encode())
    selected = importer(service, {'selected': path})
    with pytest.raises(ValueError, match='LEGACY_CONFIGURATION_INVALID'):
        selected.preview(source_id='selected', expected_revision=None)
    assert repository_empty(service)


def test_missing_legacy_secret_never_borrows_current_process_value(service, source, monkeypatch):
    path, _ = source
    path.write_text('DATABASE_URL=postgresql+psycopg://runner@database.invalid/db\n', encoding='utf-8')
    monkeypatch.setenv('JWT_SECRET_KEY', 'ambient_must_not_be_read_' + uuid4().hex)
    selected = importer(service, {'selected': path})
    preview = selected.preview(source_id='selected', expected_revision=None)
    assert 'BUSINESS_CONFIGURATION_INCOMPLETE' in preview.warnings
    assert 'JWT_SECRET_KEY' in preview.missing_fields
    assert repository_empty(service)
