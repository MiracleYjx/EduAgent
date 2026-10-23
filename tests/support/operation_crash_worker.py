"""Owned test child, blocked only after a synthetic request reservation commits."""
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.core.config import AppSettings

AppSettings.model_config["env_file"] = None


async def main():
    import httpx
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tests.contract.test_learning_operations import api, invoke, steps
    from tests.support.runtime_settings_fakes import settings_harness

    assert os.environ["EDUAGENT_ENVIRONMENT"] == "test"
    root, ready = Path(sys.argv[1]), Path(sys.argv[2])
    harness = settings_harness(root)
    await harness.activate_model("synthetic-crash-only", with_credential=True)
    engine = create_engine(os.environ["DATABASE_URL"])
    service = None
    operation = None

    async def blocked(request):
        from backend.app.models.operation_run import OperationRun
        with service.sessions() as session:
            row = session.get(OperationRun, operation.id)
            ready.write_text(json.dumps({"operation_id":str(row.id), "execution_id":str(row.execution_id),
                "environment_id":str(row.environment_id)}), "utf-8")
        await asyncio.Event().wait()
        raise AssertionError("child must be terminated by its owning test")

    module = api()
    service = module.LearningOperations(sessions=sessionmaker(engine, expire_on_commit=False),
        config=harness.service, transport=httpx.MockTransport(blocked))
    operation = service.prepare(purpose="synthetic_crash", idempotency_key=uuid4().hex, steps=steps(module))
    service.confirm(operation.id, preview_digest=operation.preview_digest)
    await service.execute(operation.id, invoke=invoke)


if __name__ == "__main__":
    asyncio.run(main())
