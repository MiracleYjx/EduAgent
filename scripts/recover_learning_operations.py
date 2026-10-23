"""Explicit local operator recovery; never resumes or resends a model request."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Coordinate one proven stopped personal executor")
    parser.add_argument("--operation-id", type=UUID, required=True)
    parser.add_argument("--execution-id", type=UUID, required=True)
    parser.add_argument("--executor-proof-root", type=Path,
        help="Explicit original root holding the same execution witness after isolated restore")
    args = parser.parse_args()
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.app.core.bootstrap import (
        build_bootstrap_security,
        resolve_bootstrap_context,
    )
    from backend.app.core.environment import resolve_environment_profile
    from backend.app.deployment.local_management import LocalManagementAuthority
    from backend.app.services.learning_operations import (
        LearningOperations,
        OperationError,
    )
    from backend.app.services.settings.management import build_settings_runtime

    profile = resolve_environment_profile()
    security = build_bootstrap_security(resolve_bootstrap_context())
    issued = LocalManagementAuthority(security).request_one_time_code()
    token = security.exchange(issued.code).token
    security.authorize(token, environment_id=profile.environment_id)
    runtime = build_settings_runtime(profile)
    engine = None
    operations = None
    try:
        runtime.credential_store()
        url, _ = runtime.personal_learning_database()
        engine = create_engine(url)
        operations = LearningOperations(sessions=sessionmaker(engine, expire_on_commit=False),
            config=runtime._require_config())
        result = operations.recover(args.operation_id, expected_execution_id=args.execution_id,
            executor_proof_root=args.executor_proof_root)
        print(result.model_dump_json(exclude={"steps"}))
        return 0
    except OperationError as error:
        print(error.code, file=sys.stderr)
        return 1
    finally:
        if operations is not None:
            asyncio.run(operations.aclose())
        if engine is not None:
            engine.dispose()
        asyncio.run(runtime.aclose())


if __name__ == "__main__":
    raise SystemExit(main())
