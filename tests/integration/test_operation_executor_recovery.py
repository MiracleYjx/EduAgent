"""Real owned PG and a killed child; no supplier requests or time-based liveness inference."""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.models.operation_run import OperationRun
from backend.app.services.learning_operations import LearningOperations, OperationError
from tests.postgres_helpers import (
    isolated_postgres_engine,
    postgres_subprocess_environment,
)


def test_killed_executor_is_explicitly_fenced_without_resending(tmp_path):
    with isolated_postgres_engine() as engine:
        root = tmp_path / "executor"
        ready = tmp_path / "ready.json"
        environment = postgres_subprocess_environment(engine.eduagent_test_manifest.database_identity.schema_name)
        worker = Path(__file__).parents[1] / "support" / "operation_crash_worker.py"
        child = subprocess.Popen([sys.executable, str(worker), str(root), str(ready)],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        service = None
        try:
            deadline = time.monotonic() + 30
            while not ready.exists():
                if child.poll() is not None:
                    pytest.fail("owned executor exited: " + child.stderr.read()[-3000:])
                if time.monotonic() > deadline:
                    pytest.fail("owned executor did not enter its synthetic request")
                time.sleep(.02)
            facts = json.loads(ready.read_text("utf-8"))
            sessions = sessionmaker(engine, expire_on_commit=False)
            config = SimpleNamespace(root=root, environment_id=UUID(facts["environment_id"]))
            service = LearningOperations(sessions=sessions, config=config)
            operation_id, execution_id = UUID(facts["operation_id"]), UUID(facts["execution_id"])
            with pytest.raises(OperationError, match="EXECUTOR_STILL_ACTIVE"):
                service.recover(operation_id, expected_execution_id=execution_id)
            child.kill()
            child.wait(timeout=10)
            restored_root = tmp_path / "restored"
            restored_root.mkdir()
            config.root = restored_root
            with pytest.raises(OperationError, match="EXECUTOR_PROOF_UNAVAILABLE"):
                service.recover(operation_id, expected_execution_id=execution_id)
            result = service.recover(operation_id, expected_execution_id=execution_id, executor_proof_root=root)
            assert result.state == "outcome_unknown"
            assert result.actual_requests == result.request_budget == 1
            assert result.steps[0]["requests"][0]["state"] == "outcome_unknown"
            with sessions() as session:
                row = session.get(OperationRun, operation_id)
                assert row.active_slot is None and row.execution_id != execution_id
            # Same explicit reconciliation is idempotent; the old operation is never reexecuted.
            assert service.recover(operation_id, expected_execution_id=execution_id).state == "outcome_unknown"
            late = service._execution.set((operation_id, execution_id, {}))
            try:
                for write in (lambda: service._release_slot(operation_id),
                    lambda: service._received(operation_id, "step-0", 0, {"status_code": 200}),
                    lambda: service._reserve(operation_id, "step-0")):
                    with pytest.raises(OperationError, match="EXECUTION_FENCED"):
                        write()
            finally:
                service._execution.reset(late)
            # A newly confirmed independent operation can take the released slot.
            other = uuid4()
            with sessions.begin() as session:
                old = session.get(OperationRun, operation_id)
                session.add(OperationRun(id=other, environment_id=config.environment_id,
                    idempotency_key=uuid4().hex, purpose="independent_confirmed_operation",
                    preview_digest="a"*64, plan_digest="b"*64, runtime_snapshot_id=old.runtime_snapshot_id,
                    state="running", request_budget=1, actual_requests=0,
                    steps=[{"step_id":"new","task_kind":"question_generation","request_budget":1,"actual_requests":0}]))
            assert service._reserve(other, "new") == 0
            with sessions() as session:
                assert session.get(OperationRun, operation_id).actual_requests == 1
                assert session.scalar(select(OperationRun.actual_requests).where(OperationRun.id == other)) == 1
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            if service is not None:
                asyncio.run(service.aclose())
