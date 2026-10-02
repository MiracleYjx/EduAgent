"""Standard PostgreSQL clients; passwords remain in subprocess environments."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from sqlalchemy.engine import URL, Connection

from backend.app.services.file_storage_service import FileStorageError


class PostgresTools:
    def __init__(self, url: URL, *, client_dir: Path | None = None, container: str | None = None,
                 container_port: int = 5432, timeout: float = 120):
        self.url = url
        self.client_dir = client_dir
        self.container = container
        self.container_port = container_port
        self.timeout = timeout

    def _command(self, program: str, database: str, args: list[str]) -> tuple[list[str], dict[str, str]]:
        environment = os.environ.copy()
        values = {"PGHOST": "127.0.0.1" if self.container else str(self.url.host or "localhost"),
                  "PGPORT": str(self.container_port if self.container else (self.url.port or 5432)),
                  "PGUSER": str(self.url.username or ""), "PGDATABASE": database,
                  "PGPASSWORD": str(self.url.password or ""), "PGCONNECT_TIMEOUT": "5",
                  "PGAPPNAME": "eduagent_offline_maintenance"}
        environment.update(values)
        if self.container:
            command = ["docker", "exec", "-i"]
            for name in values:
                command.extend(["--env", name])
            command.extend([self.container, program, *args])
        else:
            executable = str(self.client_dir / program) if self.client_dir else program
            command = [executable, *args]
        return command, environment

    def _error(self, message: bytes) -> FileStorageError:
        value = message.decode("utf-8", errors="replace")[:4000].strip()
        if self.url.password:
            value = value.replace(str(self.url.password), "[redacted]")
        return FileStorageError("POSTGRES_TOOL_FAILED", value or "PostgreSQL 客户端操作失败。")

    def validate_source(self, connection: Connection) -> None:
        if not self.container:
            return
        from sqlalchemy import text
        # Do not guess that a similarly named Docker database is this configured server.
        expected = str(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")))
        command, environment = self._command("psql", str(self.url.database),
                    ["-X", "-A", "-t", "-c", "SELECT system_identifier::text FROM pg_control_system()"])
        result = subprocess.run(command, env=environment, capture_output=True, timeout=self.timeout, check=False)
        if result.returncode != 0:
            raise self._error(result.stderr)
        if result.stdout.decode().strip() != expected:
            raise FileStorageError("POSTGRES_SERVER_MISMATCH", "指定容器与配置中的 PostgreSQL 不是同一实例。")

    def dump(self, path: Path) -> None:
        command, environment = self._command("pg_dump", str(self.url.database),
                          ["--format=custom", "--no-owner", "--no-acl", "--lock-wait-timeout=10000"])
        with path.open("xb") as output:
            result = subprocess.run(command, env=environment, stdout=output, stderr=subprocess.PIPE,
                                    timeout=self.timeout, check=False)
            output.flush()
            os.fsync(output.fileno())
        if result.returncode != 0:
            raise self._error(result.stderr)

    def restore(self, path: Path, database: str) -> None:
        command, environment = self._command("pg_restore", database,
                         ["--dbname", database, "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl"])
        with path.open("rb") as source:
            result = subprocess.run(command, env=environment, stdin=source, capture_output=True, timeout=self.timeout, check=False)
        if result.returncode != 0:
            raise self._error(result.stderr)
