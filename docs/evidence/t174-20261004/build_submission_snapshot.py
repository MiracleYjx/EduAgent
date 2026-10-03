"""Build the batch's exact runnable submission tree without touching the real index."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

repo = Path.cwd().resolve()
cache = (repo / '.cache/t170-174-20261004').resolve()
snapshot = (cache / 'submission-source').resolve()
index = (cache / 'submission-snapshot-r2.index').resolve()
assert snapshot.is_relative_to(cache) and index.is_relative_to(cache)
assert not snapshot.exists(), 'Refuse to replace an existing snapshot'
assert not index.exists(), 'Use a fresh isolated Git index'
protected = ['README.md', 'README.zh-CN.md', 'benchmark/corpus/grading_samples.json']
owned = {
    'backend/app/services/exam_scoring_service.py',
    'backend/app/services/question_scoring_invalidation.py',
    'tests/contract/test_exam_scoring_api.py',
    'tests/integration/test_exam_scoring.py',
    'tests/integration/test_exam_scoring_publication.py',
    'tests/unit/ui/test_exam_scoring_view.py',
}

def git(*args: str, env=None) -> bytes:
    return subprocess.run(['git', *args], cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout

real_index = Path(git('rev-parse', '--git-path', 'index').decode().strip())
if not real_index.is_absolute():
    real_index = repo / real_index
index_before = hashlib.sha256(real_index.read_bytes()).hexdigest()
protected_before = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in [*protected, '.env']}
previous_protected = json.loads((cache / 'protected-baseline.json').read_text(encoding='utf-8'))
assert protected_before == previous_protected, 'User protected files changed since batch baseline'
untracked = set(filter(None, git('ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')))
assert untracked == owned, f'New file inventory changed: {sorted(untracked ^ owned)}'
head = git('rev-parse', 'HEAD').decode().strip()
env = os.environ.copy()
env['GIT_INDEX_FILE'] = str(index)
git('read-tree', head, env=env)
git('add', '--update', '--', '.', env=env)
git('add', '--', *sorted(owned), env=env)
# Restore only the temporary index entries, never any user's working file.
git('restore', '--source='+head, '--staged', '--', *protected, env=env)
assert not git('ls-files', '--', '.env', env=env).strip(), 'Secrets must not enter a submission tree'
tree = git('write-tree', env=env).decode().strip()
snapshot.mkdir(parents=True)
git('-c', 'core.autocrlf=false', 'checkout-index', '--all', '--prefix='+snapshot.as_posix()+'/', env=env)
files = list(filter(None, git('ls-files', '-z', env=env).decode().split('\0')))
sha256 = {name: hashlib.sha256((snapshot/name).read_bytes()).hexdigest() for name in files}
for name in protected:
    assert (snapshot/name).read_bytes() == git('show', head+':'+name)
assert not (snapshot/'.env').exists()
assert hashlib.sha256(real_index.read_bytes()).hexdigest() == index_before, 'Real Git index changed during snapshot construction'
assert protected_before == {name: hashlib.sha256((repo/name).read_bytes()).hexdigest() for name in protected_before}
manifest = {
    'created_at': datetime.now(UTC).isoformat(),
    'parent_head': head,
    'tree': tree,
    'snapshot_path': str(snapshot),
    'temporary_index': str(index),
    'real_index_sha256_unchanged': index_before,
    'protected_worktree_sha256_unchanged': protected_before,
    'protected_snapshot_source': {name: head+':'+name for name in protected},
    'env_in_snapshot': False,
    'owned_new_files': sorted(owned),
    'file_count': len(files),
    'normal_git_clean_filters': True,
    'changed_paths': git('diff', '--name-only', head, tree).decode().splitlines(),
    'files_sha256': sha256,
}
(cache/'submission-source.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2),encoding='utf-8')
print(json.dumps({'snapshot_path': str(snapshot), 'tree': tree, 'parent_head': head, 'file_count': len(files), 'real_index_unchanged': True, 'protected_files_unchanged': True},ensure_ascii=False),flush=True)
