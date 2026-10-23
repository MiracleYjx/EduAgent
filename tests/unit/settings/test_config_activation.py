"""TCR-012: T210 observable acceptance, credential recovery and fault boundaries."""
from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from backend.app.services.settings.config_service import ConfigService
from backend.app.services.settings.probe_service import ProbeService
from backend.app.services.settings.provider_registry import ProviderRegistry
from tests.support.credential_fakes import (
    FakeFileSecurity,
    FakeProtector,
    assert_no_secret,
    baseline_values,
    make_reference,
)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 16, tzinfo=UTC)

    def __call__(self):
        return self.now


def entry(owner, name):
    method = getattr(owner, name, None)
    assert callable(method), 'T210_IMPLEMENTATION_REQUIRED: ' + name
    return method


@pytest.fixture
def repository(tmp_path):
    service = ConfigService(tmp_path, uuid4())
    try:
        yield service
    finally:
        service.close()


def open_store(service, *, protector=None, backend='dpapi', mounts=None):
    options: dict[str, Any] = {'file_security': FakeFileSecurity()}
    if backend == 'dpapi':
        options['protector'] = protector or FakeProtector()
    else:
        options['mounts'] = mounts or {}
    return entry(service, 'open_credentials')(backend=backend, **options)


def setup(service, *, guard=True, runtime=True, clock=None):
    store = open_store(service)
    registry = ProviderRegistry(environment_id=service.environment_id)
    timer = clock or Clock()
    calls = []

    async def response(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'synthetic', 'choices': [
            {'index': 0, 'message': {'role': 'assistant', 'content': '{"ok":true}'}, 'finish_reason': 'stop'}]})

    probes = ProbeService(environment_id=service.environment_id, registry=registry,
        credential_store=store, clock=timer,
        http_client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(response), **kw))
    entry(service, 'configure_admission')(registry=registry, probe_service=probes,
        business_guard=(lambda old, new: True) if guard else None,
        runtime_references=(lambda ref, version: ()) if runtime else None, clock=timer)
    jwt = store.put(purpose='jwt', secret_value=SecretStr('t210_synthetic_signing_' + uuid4().hex))
    return store, probes, timer, calls, baseline_values(service.root, service.environment_id, jwt)


def draft(service, values, expected=None):
    result = service.create_draft(values, expected_revision=expected, process_env={})
    service.validate(result.revision_id, expected_revision=expected, process_env={})
    return result


def activate(service, candidate, expected=None, ids=()):
    return service.activate(candidate.revision_id, expected_revision=expected,
                            probe_ids=ids, process_env={})


def add_text(values):
    profile_id, revision = uuid4(), uuid4()
    values['providers'] = {str(profile_id): {'profile_id': str(profile_id),
        'profile_revision': str(revision), 'display_name': 'Synthetic model',
        'adapter_id': 'openai_compatible', 'model_id': 'synthetic-text',
        'base_url': 'https://model.invalid/v1', 'auth_mode': 'none',
        'declared_capabilities': ['text', 'json_object'], 'enabled': True}}
    values['bindings'] = {'default_llm': {'task_kind': 'default_llm',
        'profile_id': str(profile_id), 'profile_revision': str(revision)}}
    return profile_id


async def evidence(probes, candidate):
    profile = next(iter(candidate.values.providers.values()))
    return [await probes.run(candidate_revision=candidate.revision_id, profile=profile,
        capability=capability, allow_billable=True) for capability in ('text', 'json_object')]


@pytest.mark.asyncio
async def test_acceptance_persists_before_pointer_and_old_acceptance_does_not_expire(repository):
    _, probes, clock, calls, values = setup(repository)
    add_text(values)
    candidate = draft(repository, values)
    assert repository.read_manifest().active_revision is None
    results = await evidence(probes, candidate)
    accepted = activate(repository, candidate, ids=[r.probe_id for r in results])
    assert accepted.activation_validation
    assert {v.capability for v in accepted.activation_validation.values()} == {'text', 'json_object'}
    assert all(v.accepted_at == clock.now for v in accepted.activation_validation.values())
    assert repository.get_revision(accepted.revision_id).activation_validation == accepted.activation_validation
    clock.now += timedelta(days=30)
    changed = accepted.values.model_dump(mode='json')
    changed['policies']['confidence_threshold'] = 0.73
    unrelated = draft(repository, changed, accepted.revision_id)
    carried = activate(repository, unrelated, accepted.revision_id)
    assert carried.activation_validation == accepted.activation_validation
    assert len(calls) == 2
    await probes.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['candidate', 'model', 'credential', 'capability', 'expired', 'failed', 'environment'])
async def test_wrong_probe_never_replaces_active(repository, monkeypatch, change):
    store, probes, clock, _, values = setup(repository)
    old = activate(repository, draft(repository, values))
    add_text(values)
    candidate = draft(repository, values, old.revision_id)
    results = await evidence(probes, candidate)
    result = results[0]
    updates = {'candidate': {'candidate_revision': uuid4()}, 'model': {'model_id': 'other'},
        'credential': {'credential_version': uuid4()}, 'capability': {'capability': 'vision'},
        'failed': {'status': 'failed'}, 'environment': {'environment_id': uuid4()}}
    if change == 'expired':
        clock.now += timedelta(minutes=10, seconds=1)
    else:
        original = probes.get
        monkeypatch.setattr(probes, 'get', lambda identifier: result.model_copy(update=updates[change])
                            if identifier == result.probe_id else original(identifier))
    before = (repository.config / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='PROBE_REQUIRED|PROBE_EXPIRED'):
        activate(repository, candidate, old.revision_id, [r.probe_id for r in results])
    assert (repository.config / 'manifest.json').read_bytes() == before
    assert store.status(old.values.business.jwt_secret_ref, old.values.business.jwt_secret_ref.version).status == 'available'
    await probes.aclose()


@pytest.mark.asyncio
async def test_business_guard_is_required_even_with_successful_probes(repository):
    _, probes, _, _, values = setup(repository, guard=False)
    add_text(values)
    candidate = draft(repository, values)
    results = await evidence(probes, candidate)
    with pytest.raises(ValueError, match='ACTIVATION_GATE_REQUIRED'):
        activate(repository, candidate, ids=[r.probe_id for r in results])
    assert repository.read_manifest().active_revision is None
    await probes.aclose()


@pytest.mark.asyncio
async def test_adding_vision_binding_only_requires_its_new_candidate_probe(repository):
    """T083 TCR: exercise real admission and snapshots with offline HTTP only."""
    from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService

    store, probes, clock, calls, values = setup(repository)
    profile_id = add_text(values)
    credential = store.put(purpose='provider', secret_value=SecretStr('t083_synthetic_' + uuid4().hex))
    values['providers'][str(profile_id)].update(
        adapter_id='deepseek', model_id='deepseek-flash',
        base_url='https://api.deepseek.com',
        auth_mode='api_key', credential_ref=credential.model_dump(mode='json'),
        declared_capabilities=['text', 'json_object', 'vision'],
    )
    first = draft(repository, values)
    results = await evidence(probes, first)
    profile = first.values.providers[str(profile_id)]
    old_vision = await probes.run(candidate_revision=first.revision_id, profile=profile,
        capability='vision', allow_billable=True)
    accepted = activate(repository, first, ids=[result.probe_id for result in results])
    assert set(accepted.activation_validation) == {'default_llm:text', 'default_llm:json_object'}
    snapshots = RuntimeSnapshotService(repository, clock=clock)
    old_snapshot = snapshots.create(workflow_id='synthetic-before-vision', request_id='before',
        task_kinds=['default_llm'])

    clock.now += timedelta(days=1)
    changed = accepted.values.model_dump(mode='json')
    changed['bindings']['vision'] = {'task_kind': 'vision',
        'profile_id': str(profile_id), 'profile_revision': str(profile.profile_revision)}
    candidate = draft(repository, changed, accepted.revision_id)
    before_targets = repository._ai_targets(accepted.values, accepted.revision_id)
    after_targets = repository._ai_targets(candidate.values, candidate.revision_id)
    assert set(after_targets) - set(before_targets) == {'vision:vision'}
    assert all(after_targets[key][0] == facts[0] for key, facts in before_targets.items())
    assert candidate.values.providers == accepted.values.providers

    for ids in ((), (old_vision.probe_id,)):
        with pytest.raises(ValueError, match='PROBE_REQUIRED'):
            activate(repository, candidate, accepted.revision_id, ids)
        assert repository.read_manifest().active_revision == accepted.revision_id
        assert len(calls) == 3

    new_vision = await probes.run(candidate_revision=candidate.revision_id,
        profile=profile, capability='vision', allow_billable=True)
    current = activate(repository, candidate, accepted.revision_id, [new_vision.probe_id])
    assert len(calls) == 4
    assert set(current.activation_validation) == set(accepted.activation_validation) | {'vision:vision'}
    assert all(current.activation_validation[key] == record
        for key, record in accepted.activation_validation.items())
    assert current.activation_validation['vision:vision'].probe_id == new_vision.probe_id

    new_snapshot = snapshots.create(workflow_id='synthetic-after-vision', request_id='after',
        task_kinds=['default_llm', 'vision'])
    assert new_snapshot.settings_revision == current.revision_id
    assert new_snapshot.resolved_task_bindings['vision'].profile_revision == profile.profile_revision
    assert new_snapshot.resolved_task_bindings['default_llm'] == old_snapshot.resolved_task_bindings['default_llm']
    assert snapshots.load(old_snapshot.snapshot_id) == old_snapshot
    assert old_snapshot.settings_revision == accepted.revision_id
    assert 'vision' not in old_snapshot.resolved_task_bindings
    assert snapshots.active_lease_count == 0
    await probes.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['model_id', 'base_url', 'parameters', 'credential_ref'])
async def test_reused_profile_revision_cannot_inherit_acceptance_for_changed_identity(repository, field):
    store, probes, _, _, values = setup(repository)
    profile_id = add_text(values)
    candidate = draft(repository, values)
    accepted = activate(repository, candidate, ids=[r.probe_id for r in await evidence(probes, candidate)])
    changed = accepted.values.model_dump(mode='json')
    profile = changed['providers'][str(profile_id)]
    if field == 'credential_ref':
        ref = store.put(purpose='provider', secret_value=SecretStr('t210_synthetic_' + uuid4().hex))
        profile.update(auth_mode='api_key', credential_ref=ref.model_dump(mode='json'))
    else:
        profile[field] = {'model_id': 'different', 'base_url': 'https://other.invalid/v1',
                          'parameters': {'temperature': 0.3}}[field]
    revised = draft(repository, changed, accepted.revision_id)
    with pytest.raises(ValueError, match='PROBE_REQUIRED|PROFILE_REVISION'):
        activate(repository, revised, accepted.revision_id)
    assert repository.read_manifest().active_revision == accepted.revision_id
    await probes.aclose()


@pytest.mark.asyncio
async def test_pending_reopen_preserves_acceptance_without_reprobe(repository):
    store, probes, clock, calls, values = setup(repository)
    add_text(values)
    candidate = draft(repository, values)
    first = activate(repository, candidate, ids=[r.probe_id for r in await evidence(probes, candidate)])
    changed = first.values.model_dump(mode='json')
    changed['business']['jwt_expire_minutes'] = 81
    candidate2 = draft(repository, changed, first.revision_id)
    pending = activate(repository, candidate2, first.revision_id)
    assert pending.status == 'pending_restart'
    assert repository.read_manifest().active_revision == first.revision_id
    clock.now += timedelta(days=2)
    assert repository.get_revision(pending.revision_id).activation_validation == first.activation_validation
    reference = first.values.business.jwt_secret_ref
    assert store.status(reference, reference.version).status == 'available'
    # Process shutdown closes the owned backend; the next process only reads.
    repository.close()
    with ConfigService(repository.root, repository.environment_id, writable=False) as reader:
        assert reader.read_manifest().pending_restart_revision == pending.revision_id
        assert reader.get_revision(pending.revision_id).activation_validation == pending.activation_validation
    assert len(calls) == 2
    await probes.aclose()


@pytest.mark.parametrize('fault', ['revision', 'manifest'])
def test_credential_then_configuration_fault_preserves_old_active(repository, monkeypatch, fault):
    store, _, _, _, values = setup(repository)
    old = activate(repository, draft(repository, values))
    original = store.get(old.values.business.jwt_secret_ref, old.values.business.jwt_secret_ref.version)
    new = store.put(purpose='jwt', secret_value=SecretStr('t210_replacement_' + uuid4().hex))
    changed = old.values.model_dump(mode='json')
    changed['business']['jwt_secret_ref'] = new.model_dump(mode='json')
    candidate = draft(repository, changed, old.revision_id)
    before = (repository.config / 'manifest.json').read_bytes()
    write = repository._atomic_write
    def fail(path, payload, stage):
        if stage == fault:
            raise OSError('synthetic-sensitive-write-failure')
        return write(path, payload, stage)
    monkeypatch.setattr(repository, '_atomic_write', fail)
    with pytest.raises(ValueError, match='CONFIG_IO_ERROR'):
        activate(repository, candidate, old.revision_id)
    assert (repository.config / 'manifest.json').read_bytes() == before
    assert store.get(old.values.business.jwt_secret_ref, old.values.business.jwt_secret_ref.version) == original
    assert store.status(new, new.version).status == 'available'


@pytest.mark.parametrize('state', ['absent', 'invalid', 'revoked'])
def test_unavailable_credential_blocks_publication_and_restore(repository, state):
    store, _, _, _, values = setup(repository)
    old = activate(repository, draft(repository, values))
    ref = store.put(purpose='jwt', secret_value=SecretStr('t210_unavailable_' + uuid4().hex))
    if state == 'absent':
        store.delete(ref, ref.version)
    else:
        entry(store, 'revoke' if state == 'revoked' else 'mark_invalid')(ref, ref.version)
    assert store.status(ref, ref.version).status == state
    changed = old.values.model_dump(mode='json')
    changed['business']['jwt_secret_ref'] = ref.model_dump(mode='json')
    candidate = draft(repository, changed, old.revision_id)
    before = (repository.config / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='CREDENTIAL_UNAVAILABLE'):
        activate(repository, candidate, old.revision_id)
    assert (repository.config / 'manifest.json').read_bytes() == before
    entry(store, 'revoke')(old.values.business.jwt_secret_ref, old.values.business.jwt_secret_ref.version)
    with pytest.raises(ValueError, match='CREDENTIAL_UNAVAILABLE'):
        repository.restore_revision(old.revision_id, expected_revision=old.revision_id)
    assert (repository.config / 'manifest.json').read_bytes() == before


def test_weak_jwt_secret_blocks_activation_before_manifest_write(repository):
    store, _, _, _, values = setup(repository)
    weak = store.put(purpose='jwt', secret_value=SecretStr('t210_short_jwt'))
    values['business']['jwt_secret_ref'] = weak.model_dump(mode='json')
    candidate = draft(repository, values)
    with pytest.raises(ValueError, match='JWT_SECRET_TOO_WEAK'):
        activate(repository, candidate)
    assert repository.read_manifest().active_revision is None
    assert not (repository.config / 'manifest.json').exists()


def test_weak_jwt_secret_in_existing_revision_blocks_restore(repository):
    store, _, _, _, values = setup(repository)
    active = activate(repository, draft(repository, values))
    weak = store.put(purpose='jwt', secret_value=SecretStr('t210_short_jwt'))
    changed = active.values.model_dump(mode='json')
    changed['business']['jwt_secret_ref'] = weak.model_dump(mode='json')
    legacy = active.model_copy(update={
        'revision_id': uuid4(), 'parent_revision': active.revision_id,
        'values': active.values.model_validate(changed),
        'status': 'active', 'activation_validation': {},
    })
    repository._persist(repository.config / 'revisions' / f'{legacy.revision_id}.json',
                        legacy.model_dump(mode='json'), 'revision')
    before = (repository.config / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='JWT_SECRET_TOO_WEAK'):
        repository.restore_revision(legacy.revision_id, expected_revision=active.revision_id)
    assert (repository.config / 'manifest.json').read_bytes() == before


@pytest.mark.parametrize('use', ['active', 'pending', 'unknown', 'nonterminal', 'terminal'])
def test_reference_index_uses_manifest_and_requires_complete_runtime_facts(repository, use):
    store, probes, clock, _, values = setup(repository, runtime=use != 'unknown')
    active = activate(repository, draft(repository, values))
    oldref = active.values.business.jwt_secret_ref
    ref = oldref if use == 'active' else store.put(purpose='jwt', secret_value=SecretStr('t210_candidate_' + uuid4().hex))
    if use == 'pending':
        changed = active.values.model_dump(mode='json')
        changed['business']['jwt_secret_ref'] = ref.model_dump(mode='json')
        activate(repository, draft(repository, changed, active.revision_id), active.revision_id)
    if use in {'nonterminal', 'terminal'}:
        entry(repository, 'configure_admission')(registry=probes.registry, probe_service=probes,
            business_guard=lambda old, new: True, runtime_references=lambda r, v: (
                'nonterminal_snapshot' if use == 'nonterminal' else 'terminal_history',), clock=clock)
    if use == 'terminal':
        store.delete(ref, ref.version)
        assert store.status(ref, ref.version).status == 'absent'
    else:
        with pytest.raises(RuntimeError, match='REFERENCE_INDEX_UNAVAILABLE' if use == 'unknown' else 'IN_USE'):
            store.delete(ref, ref.version)
        assert store.status(ref, ref.version).status == 'available'


def test_delete_holds_common_transaction_until_unlink(repository, monkeypatch):
    store, _, _, _, _ = setup(repository)
    ref = store.put(purpose='jwt', secret_value=SecretStr('t210_unused_' + uuid4().hex))
    checked, release, acquired = threading.Event(), threading.Event(), threading.Event()
    original = repository.references
    def observed(reference, version):
        facts = original(reference, version)
        checked.set()
        assert release.wait(3)
        return facts
    monkeypatch.setattr(repository, 'references', observed)
    def compete():
        with entry(repository, 'reference_transaction')():
            acquired.set()
    # A rival publication must wait while the deletion still holds the shared lock.
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(store.delete, ref, ref.version)
        assert checked.wait(3)
        rival = pool.submit(compete)
        assert not acquired.wait(0.05)
        release.set()
        deleting.result(timeout=5)
        rival.result(timeout=5)
    assert acquired.is_set()
    assert store.status(ref, ref.version).status == 'absent'


@pytest.mark.parametrize('backend', ['dpapi', 'mounted_secret'])
@pytest.mark.parametrize('damage', ['root', 'binding', 'receipt'])
def test_reopen_never_reenrolls_old_reference_after_vault_loss(repository, tmp_path, backend, damage):
    protector = FakeProtector()
    mount = tmp_path / 'external-secret'
    mount.write_text('t210_original_' + uuid4().hex, encoding='utf-8')
    ref = make_reference(repository.environment_id, backend=backend, secret_id='jwt-v1' if backend != 'dpapi' else None)
    mounts = {'jwt-v1': (ref, mount)} if backend != 'dpapi' else None
    store = open_store(repository, protector=protector, backend=backend, mounts=mounts)
    if backend == 'dpapi':
        ref = store.put(purpose='jwt', secret_value=SecretStr('t210_original_' + uuid4().hex))
    candidate = repository.create_draft(baseline_values(repository.root, repository.environment_id, ref), expected_revision=None, process_env={})
    assert candidate.values.business.jwt_secret_ref == ref
    # Process shutdown closes the owned backend before the vault is damaged.
    repository.close()
    if damage == 'root':
        shutil.rmtree(tmp_path / 'credentials')  # Only this test-owned vault.
    elif damage == 'binding':
        (tmp_path / 'credentials' / ('environment.json' if backend == 'dpapi' else 'mounted-environment.json')).unlink()
    else:
        (tmp_path / 'config' / 'credential-backend.json').write_text('{damaged', encoding='utf-8')
    mount.write_text('t210_replaced_' + uuid4().hex, encoding='utf-8')
    with ConfigService(tmp_path, repository.environment_id) as reopened, pytest.raises(
            ValueError, match='CREDENTIAL_RECOVERY_REQUIRED'):
        open_store(reopened, protector=protector, backend=backend, mounts=mounts)
    if damage == 'root':
        assert not (tmp_path / 'credentials').exists()


def test_marking_invalid_or_revoked_never_leaks_or_erases_original(repository):
    store = open_store(repository)
    marker = 't210_private_' + uuid4().hex
    ref = store.put(purpose='provider', secret_value=SecretStr(marker))
    original = {p.name: p.read_bytes() for p in store.credentials.glob('*.dpapi')}
    entry(store, 'mark_invalid')(ref, ref.version)
    assert store.status(ref, ref.version).status == 'invalid'
    entry(store, 'revoke')(ref, ref.version)
    assert store.status(ref, ref.version).status == 'revoked'
    with pytest.raises(RuntimeError) as error:
        store.get(ref, ref.version)
    assert {p.name: p.read_bytes() for p in store.credentials.glob('*.dpapi')} == original
    assert_no_secret([marker], {'error': str(error.value), 'status': store.status(ref, ref.version).model_dump_json(),
        'metadata': b''.join(p.read_bytes() for p in store.credentials.glob('*.json'))})
