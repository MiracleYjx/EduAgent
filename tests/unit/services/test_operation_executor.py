"""Executor identity is a kernel-held lifetime witness, never an elapsed timeout."""
import subprocess
import sys
from uuid import uuid4

import pytest


def test_live_executor_cannot_be_recovered_even_with_reused_pid_metadata(tmp_path):
    from backend.app.services.operation_executor import ExecutorError, ExecutorGuard

    with ExecutorGuard.create(tmp_path, uuid4()) as guard:
        with pytest.raises(ExecutorError, match="EXECUTOR_STILL_ACTIVE"), ExecutorGuard.stopped(tmp_path, guard.identity):
            pass
        changed = dict(guard.identity, process_start="different-process")
        with pytest.raises(ExecutorError, match="EXECUTOR_STILL_ACTIVE"), ExecutorGuard.stopped(tmp_path, changed):
            pass


def test_replaced_or_missing_witness_is_not_proof(tmp_path):
    from backend.app.services.operation_executor import ExecutorError, ExecutorGuard

    with ExecutorGuard.create(tmp_path, uuid4()) as guard:
        identity = dict(guard.identity)
    guard.path.unlink()
    with pytest.raises(ExecutorError, match="EXECUTOR_PROOF_UNAVAILABLE"), ExecutorGuard.stopped(tmp_path, identity):
        pass


def test_real_child_termination_releases_execution_witness(tmp_path):
    import json

    from backend.app.services.operation_executor import ExecutorError, ExecutorGuard

    script = (
        "import json,time; from pathlib import Path; from uuid import uuid4; "
        "from backend.app.services.operation_executor import ExecutorGuard; "
        f"g=ExecutorGuard.create(Path({str(tmp_path)!r}),uuid4()); g.__enter__(); "
        "print(json.dumps(g.identity),flush=True); time.sleep(90)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        identity = json.loads(child.stdout.readline())
        with pytest.raises(ExecutorError, match="EXECUTOR_STILL_ACTIVE"), ExecutorGuard.stopped(tmp_path, identity):
            pass
        child.kill()
        child.wait(timeout=10)
        with ExecutorGuard.stopped(tmp_path, identity):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
