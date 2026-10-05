"""Record actual build environment and bundle; hashes are for traceability."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from importlib.metadata import distributions
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--package", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
files = sorted(
    p
    for p in args.package.rglob("*")
    if p.is_file() and p.resolve() != args.output.resolve()
)
versions = {
    d.metadata["Name"]: d.version for d in distributions() if d.metadata["Name"]
}
commit = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
).stdout.strip()
with (args.package / "EduAgent.exe").open("rb") as stream:
    executable_hash = hashlib.file_digest(stream, "sha256").hexdigest()
source_paths = sorted(
    set(root.glob("backend/**/*.py"))
    | set(root.glob("migrations/**/*.py"))
    | {
        root / "scripts/launch_windows.py",
        root / "packaging/EduAgent.spec",
        root / "packaging/requirements-windows.txt",
        root / "config/windows.env.example",
        root / "pyproject.toml",
    }
)
source_hashes = {
    str(p.relative_to(root))
    .replace("\\", "/"): hashlib.sha256(p.read_bytes())
    .hexdigest()
    for p in source_paths
}
source_dirty = bool(
    subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
)
result = {
    "built_at": datetime.now(UTC).isoformat(),
    "source_commit": commit,
    "source_dirty": source_dirty,
    "source_files_sha256": source_hashes,
    "python": platform.python_version(),
    "platform": platform.platform(),
    "mode": "onedir",
    "dependencies": dict(sorted(versions.items())),
    "file_count": len(files),
    "size_bytes": sum(p.stat().st_size for p in files),
    "executable_sha256": executable_hash,
    "external": "config, database, Redis, business files and model weights",
    "identity_policy": "Build versions/hashes are traceability only; no runtime equality gate.",
}
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
print(
    json.dumps(
        {k: result[k] for k in ("source_commit", "python", "file_count", "size_bytes")},
        ensure_ascii=False,
    )
)
