"""Only this batch's isolated validation resources; connection secrets stay in env."""
import json, os, subprocess, sys, time
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path.cwd()))
from backend.app.core.config import get_settings

run = Path(".cache/t170-174-20261004").resolve()
record = json.loads((run / "isolation.json").read_text(encoding="utf-8"))
assert record["database"].startswith("eduagent_e4_batch_")
url = make_url(str(get_settings().database_url))
env = os.environ.copy()
from dotenv import dotenv_values
env.update({k: v for k, v in dotenv_values(Path.cwd()/'.env').items() if v is not None})
env['PYTHONPATH'] = str(run/sys.argv[1])
env["DATABASE_URL"] = url.set(database=record["database"]).render_as_string(hide_password=False)
env["REDIS_URL"] = f'redis://127.0.0.1:{record["redis_port"]}/0'
env["STORAGE_ROOT"] = str(run / "business-files")
env["EDUAGENT_TEST_PG_CONTAINER"] = record["pg_container"]
env["PYTHONIOENCODING"] = "utf-8"
snapshot = sys.argv[1]
assert snapshot in {"submission-source"}
label = sys.argv[2]
assert label.isidentifier()
start = datetime.now(UTC)
clock = time.perf_counter()
with (run / f"{label}.log").open("wb") as stream:
    result = subprocess.run([sys.executable, *sys.argv[3:]], cwd=run/sys.argv[1], env=env, stdout=stream, stderr=subprocess.STDOUT)
report = {"label": label, "exit_code": result.returncode, "started_at": start.isoformat(), "seconds": round(time.perf_counter() - clock, 3)}
(run / f"{label}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report), flush=True)
raise SystemExit(result.returncode)
