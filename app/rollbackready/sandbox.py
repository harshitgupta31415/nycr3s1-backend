from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Self

from app.core.config import settings
from app.rollbackready.contracts import SnapshotSummary
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.sql import sanitize_database_error

SANDBOX_DATABASE = "rollbackready"
ADMIN_USER = "rr_admin"
MIGRATION_USER = "rr_migrator"
COMMAND_TIMEOUT_SECONDS = 90

_simulation_lock = Lock()


@dataclass(frozen=True, slots=True)
class DatabaseExecution:
    succeeded: bool
    duration_ms: int
    output: str
    sanitized_error: str | None
    affected_rows: int | None


@dataclass(frozen=True, slots=True)
class ScriptExecution:
    execution: DatabaseExecution
    completed_statement_indexes: tuple[int, ...]
    failed_statement_index: int | None
    statement_outputs: dict[int, str]


class PostgresSandbox:
    """Disposable PostgreSQL 18 process with no production credentials."""

    def __init__(self, analysis_id: str) -> None:
        self.analysis_id = analysis_id
        self._deadline = time.monotonic() + max(
            1, settings.rollbackready_total_runtime_seconds
        )
        self._binaries: dict[str, str] = {}
        self._backend = self._resolve_backend()
        self._root: Path | None = None
        self._data: Path | None = None
        self._socket: Path | None = None
        self._container: str | None = None
        self._started = False

    def __enter__(self) -> Self:
        try:
            if self._backend == "native":
                self._start_native()
            elif self._backend == "docker":
                self._start_docker()
            else:
                raise RollbackReadyError(
                    "SANDBOX_UNAVAILABLE",
                    "PostgreSQL 18 sandbox execution is not available on this runtime.",
                    status_code=503,
                    analysis_id=self.analysis_id,
                )
            self._create_migration_role()
            self.reset_database()
            self._started = True
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, *_: object) -> None:
        self.close()

    def _resolve_backend(self) -> str:
        configured = settings.rollbackready_sandbox_backend
        if configured not in {"auto", "native", "docker", "disabled"}:
            raise RollbackReadyError(
                "INVALID_SANDBOX_CONFIGURATION",
                "ROLLBACKREADY_SANDBOX_BACKEND must be auto, native, docker, or disabled.",
                status_code=500,
            )
        if configured == "disabled":
            return "disabled"
        if configured in {"auto", "native"} and self._find_native_binaries():
            return "native"
        if configured == "native":
            return "disabled"
        if configured in {"auto", "docker"} and shutil.which("docker"):
            return "docker"
        return "disabled"

    def _find_native_binaries(self) -> bool:
        configured = settings.rollbackready_postgres_bin
        binary_root = Path(configured).resolve() if configured else None
        resolved: dict[str, str] = {}
        for name in ("initdb", "pg_ctl", "psql"):
            suffix = ".exe" if os.name == "nt" else ""
            candidate = binary_root / f"{name}{suffix}" if binary_root else None
            found = str(candidate) if candidate and candidate.is_file() else shutil.which(name)
            if not found:
                return False
            resolved[name] = found
        self._binaries = resolved
        return True

    def _start_native(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix=f"rollbackready-{self.analysis_id[:8]}-"))
        self._data = self._root / "data"
        self._socket = self._root / "socket"
        self._socket.mkdir(mode=0o700)
        self._run(
            [
                self._binaries["initdb"],
                "--pgdata",
                str(self._data),
                "--username",
                ADMIN_USER,
                "--auth-local=trust",
                "--auth-host=reject",
                "--encoding=UTF8",
                "--no-locale",
                "--no-sync",
            ]
        )
        options = " ".join(
            [
                "-F",
                f'-k "{self._socket}"',
                "-h ''",
                "-p 5432",
                "-c max_connections=8",
                "-c shared_buffers=64MB",
                "-c work_mem=4MB",
                "-c temp_file_limit=262144",
                "-c fsync=off",
                "-c synchronous_commit=off",
                "-c log_statement=none",
                "-c logging_collector=off",
            ]
        )
        self._run(
            [
                self._binaries["pg_ctl"],
                "start",
                "--pgdata",
                str(self._data),
                "--wait",
                "--timeout=30",
                "--log",
                str(self._root / "postgres.log"),
                "--options",
                options,
                "--silent",
            ]
        )

    def _start_docker(self) -> None:
        self._container = f"rr-{self.analysis_id[:8]}-{secrets.token_hex(3)}"
        self._run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                self._container,
                "--network",
                "none",
                "--cpus",
                "1.0",
                "--memory",
                "1024m",
                "--memory-swap",
                "1024m",
                "--pids-limit",
                "128",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/var/lib/postgresql:rw,noexec,nosuid,size=768m",
                "--env",
                f"POSTGRES_USER={ADMIN_USER}",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                "POSTGRES_DB=postgres",
                settings.rollbackready_postgres_image,
                "-c",
                "max_connections=8",
                "-c",
                "shared_buffers=64MB",
                "-c",
                "work_mem=4MB",
                "-c",
                "temp_file_limit=262144",
                "-c",
                "fsync=off",
                "-c",
                "synchronous_commit=off",
                "-c",
                "log_statement=none",
            ],
            timeout=120,
        )
        deadline = min(time.monotonic() + 45, self._deadline)
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["docker", "exec", self._container, "pg_isready", "-U", ADMIN_USER],
                capture_output=True,
                text=True,
                timeout=self._remaining_timeout(5),
                check=False,
            )
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise RollbackReadyError(
            "SANDBOX_START_TIMEOUT",
            "The disposable PostgreSQL sandbox did not become ready in time.",
            status_code=503,
            analysis_id=self.analysis_id,
        )

    def _create_migration_role(self) -> None:
        self._psql(
            "CREATE ROLE rr_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;",
            database="postgres",
            user=ADMIN_USER,
        )

    def reset_database(self) -> None:
        self._psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{SANDBOX_DATABASE}' AND pid <> pg_backend_pid();\n"
            f"DROP DATABASE IF EXISTS {SANDBOX_DATABASE};\n"
            f"CREATE DATABASE {SANDBOX_DATABASE} OWNER {MIGRATION_USER};",
            database="postgres",
            user=ADMIN_USER,
        )

    def execute(self, sql: str, *, wrap_rollback: bool = False) -> DatabaseExecution:
        script = f"BEGIN;\n{sql.rstrip(';')};\nROLLBACK;" if wrap_rollback else sql
        started = time.monotonic()
        try:
            completed = self._psql(
                script,
                database=SANDBOX_DATABASE,
                user=MIGRATION_USER,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return DatabaseExecution(
                succeeded=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                output="",
                sanitized_error="Statement execution exceeded the sandbox time limit.",
                affected_rows=None,
            )
        duration = int((time.monotonic() - started) * 1000)
        output = completed.stdout.strip()
        error = completed.stderr.strip()
        return DatabaseExecution(
            succeeded=completed.returncode == 0,
            duration_ms=duration,
            output=output,
            sanitized_error=sanitize_database_error(error) if completed.returncode else None,
            affected_rows=_affected_rows(output),
        )

    def execute_statements(self, statements: list[str]) -> ScriptExecution:
        """Execute one migration script while retaining per-statement evidence."""
        token = secrets.token_hex(8)
        prefix = f"ROLLBACKREADY_{token}_"
        script = "\n".join(
            f"\\echo {prefix}START_{index}\n"
            f"{statement.rstrip(';')};\n"
            f"\\echo {prefix}END_{index}"
            for index, statement in enumerate(statements, start=1)
        )
        started = time.monotonic()
        try:
            completed = self._psql(
                script,
                database=SANDBOX_DATABASE,
                user=MIGRATION_USER,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            return_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_text(exc.stdout)
            stderr = _timeout_text(exc.stderr)
            return_code = 124

        completed_indexes: list[int] = []
        started_indexes: list[int] = []
        statement_outputs: dict[int, list[str]] = {}
        clean_output: list[str] = []
        current_index: int | None = None
        marker = re.compile(
            rf"^{re.escape(prefix)}(?P<position>START|END)_(?P<index>\d+)$"
        )
        for line in stdout.splitlines():
            match = marker.fullmatch(line.strip())
            if match:
                index = int(match.group("index"))
                if match.group("position") == "START":
                    started_indexes.append(index)
                    current_index = index
                    statement_outputs.setdefault(index, [])
                else:
                    completed_indexes.append(index)
                    current_index = None
                continue
            clean_output.append(line)
            if current_index is not None:
                statement_outputs.setdefault(current_index, []).append(line)

        failed_index = None
        if return_code:
            failed_index = next(
                (
                    index
                    for index in started_indexes
                    if index not in completed_indexes
                ),
                min(len(completed_indexes) + 1, len(statements)),
            )
        output = "\n".join(clean_output).strip()
        error = (
            "Statement execution exceeded the sandbox time limit."
            if return_code == 124
            else sanitize_database_error(stderr.strip()) if return_code else None
        )
        execution = DatabaseExecution(
            succeeded=return_code == 0,
            duration_ms=int((time.monotonic() - started) * 1000),
            output=output,
            sanitized_error=error,
            affected_rows=_affected_rows(output),
        )
        return ScriptExecution(
            execution=execution,
            completed_statement_indexes=tuple(completed_indexes),
            failed_statement_index=failed_index,
            statement_outputs={
                index: "\n".join(lines).strip()
                for index, lines in statement_outputs.items()
            },
        )

    def snapshot(
        self,
        *,
        content_columns: dict[str, list[str]] | None = None,
    ) -> SnapshotSummary:
        schema_query = """
            SELECT 'column|' || table_schema || '.' || table_name || '|' ||
                   ordinal_position || '|' || column_name || '|' || data_type || '|' ||
                   is_nullable || '|' || COALESCE(column_default, '')
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            UNION ALL
            SELECT 'constraint|' || tc.table_schema || '.' || tc.table_name || '|' ||
                   tc.constraint_type || '|' || tc.constraint_name
            FROM information_schema.table_constraints tc
            WHERE tc.table_schema NOT IN ('pg_catalog', 'information_schema')
            UNION ALL
            SELECT 'index|' || schemaname || '.' || tablename || '|' || indexname || '|' || indexdef
            FROM pg_indexes
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            UNION ALL
            SELECT 'view|' || table_schema || '.' || table_name || '|' ||
                   COALESCE(view_definition, '')
            FROM information_schema.views
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            UNION ALL
            SELECT 'sequence|' || sequence_schema || '.' || sequence_name || '|' ||
                   data_type || '|' || start_value || '|' || minimum_value || '|' ||
                   maximum_value || '|' || increment || '|' || cycle_option
            FROM information_schema.sequences
            WHERE sequence_schema NOT IN ('pg_catalog', 'information_schema')
            UNION ALL
            SELECT 'enum|' || namespace.nspname || '.' || enum_type.typname || '|' ||
                   enum_value.enumsortorder || '|' || enum_value.enumlabel
            FROM pg_type enum_type
            JOIN pg_enum enum_value ON enum_value.enumtypid = enum_type.oid
            JOIN pg_namespace namespace ON namespace.oid = enum_type.typnamespace
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            UNION ALL
            SELECT 'trigger|' || trigger_schema || '.' || event_object_table || '|' ||
                   trigger_name || '|' || event_manipulation || '|' || action_timing || '|' ||
                   action_statement
            FROM information_schema.triggers
            WHERE trigger_schema NOT IN ('pg_catalog', 'information_schema')
            UNION ALL
            SELECT 'function|' || namespace.nspname || '.' || procedure.proname || '|' ||
                   pg_get_function_identity_arguments(procedure.oid) || '|' ||
                   pg_get_functiondef(procedure.oid)
            FROM pg_proc procedure
            JOIN pg_namespace namespace ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND procedure.prokind IN ('f', 'p')
            UNION ALL
            SELECT 'policy|' || schemaname || '.' || tablename || '|' || policyname || '|' ||
                   permissive || '|' || roles::text || '|' || cmd || '|' ||
                   COALESCE(qual, '') || '|' || COALESCE(with_check, '')
            FROM pg_policies
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            UNION ALL
            SELECT 'privilege|' || table_schema || '.' || table_name || '|' ||
                   grantee || '|' || privilege_type || '|' || is_grantable
            FROM information_schema.table_privileges
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY 1;
        """
        schema = self._psql(
            schema_query,
            database=SANDBOX_DATABASE,
            user=MIGRATION_USER,
        ).stdout.strip()
        compatibility_schema = "\n".join(
            _compatibility_schema_line(line)
            for line in schema.splitlines()
        )
        table_lines = self._psql(
            "SELECT table_schema || '|' || table_name FROM information_schema.tables "
            "WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema') "
            "ORDER BY table_schema, table_name;",
            database=SANDBOX_DATABASE,
            user=MIGRATION_USER,
        ).stdout.splitlines()
        row_counts: dict[str, int] = {}
        content_hashes: dict[str, str] = {}
        captured_columns: dict[str, list[str]] = {}
        for line in table_lines:
            if "|" not in line:
                continue
            namespace, table = line.strip().split("|", 1)
            quoted = f'{_quote_identifier(namespace)}.{_quote_identifier(table)}'
            count_output = self._psql(
                f"SELECT COUNT(*) FROM {quoted};",
                database=SANDBOX_DATABASE,
                user=MIGRATION_USER,
            ).stdout.strip()
            table_key = f"{namespace}.{table}"
            row_counts[table_key] = int(count_output.splitlines()[-1])
            if sum(row_counts.values()) > max(
                1, settings.rollbackready_max_total_rows
            ):
                raise RollbackReadyError(
                    "ROW_LIMIT_EXCEEDED",
                    "Synthetic fixtures exceed the configured sandbox row limit.",
                    status_code=413,
                    analysis_id=self.analysis_id,
                    details={
                        "limit": settings.rollbackready_max_total_rows,
                    },
                )
            available_columns = [
                column.strip()
                for column in self._psql(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_schema = {_sql_literal(namespace)} "
                    f"AND table_name = {_sql_literal(table)} "
                    "ORDER BY ordinal_position;",
                    database=SANDBOX_DATABASE,
                    user=MIGRATION_USER,
                ).stdout.splitlines()
                if column.strip()
            ]
            selected_columns = (
                list(content_columns.get(table_key, []))
                if content_columns is not None
                else available_columns
            )
            captured_columns[table_key] = selected_columns
            if any(column not in available_columns for column in selected_columns):
                content_hashes[table_key] = "__missing_baseline_column__"
                continue
            row_expression = ", ".join(
                f"row_data.{_quote_identifier(column)}"
                for column in selected_columns
            )
            content_output = self._psql(
                "SELECT COALESCE(string_agg(encoded_row, E'\\n' ORDER BY encoded_row), '') "
                "FROM (SELECT jsonb_build_array("
                f"{row_expression})::text AS encoded_row FROM {quoted} AS row_data) rows;",
                database=SANDBOX_DATABASE,
                user=MIGRATION_USER,
            ).stdout.strip()
            content_hashes[table_key] = hashlib.sha256(
                content_output.encode("utf-8")
            ).hexdigest()
        return SnapshotSummary(
            schema_hash=hashlib.sha256(schema.encode("utf-8")).hexdigest(),
            compatibility_schema_hash=hashlib.sha256(
                compatibility_schema.encode("utf-8")
            ).hexdigest(),
            table_count=len(row_counts),
            row_counts=row_counts,
            content_hashes=content_hashes,
            content_columns=captured_columns,
        )

    def _psql(
        self,
        sql: str,
        *,
        database: str,
        user: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        pgoptions = (
            f"-c statement_timeout={settings.rollbackready_statement_timeout_ms} "
            f"-c lock_timeout={settings.rollbackready_lock_timeout_ms}"
        )
        common = [
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--set=VERBOSITY=terse",
            "--tuples-only",
            "--no-align",
            "--username",
            user,
            "--dbname",
            database,
        ]
        environment = os.environ.copy()
        environment["PGOPTIONS"] = pgoptions
        if self._backend == "docker":
            if not self._container:
                raise RuntimeError("Docker sandbox was not initialized")
            command = [
                "docker",
                "exec",
                "--interactive",
                "--env",
                f"PGOPTIONS={pgoptions}",
                self._container,
                "psql",
                *common,
            ]
        else:
            if not self._socket:
                raise RuntimeError("Native sandbox was not initialized")
            command = [
                self._binaries["psql"],
                "--host",
                str(self._socket),
                "--port",
                "5432",
                *common,
            ]
        return subprocess.run(
            command,
            input=sql,
            capture_output=True,
            text=True,
            timeout=self._remaining_timeout(COMMAND_TIMEOUT_SECONDS),
            check=check,
            env=environment,
        )

    def _run(
        self, command: list[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self._remaining_timeout(timeout),
            check=True,
        )

    def _remaining_timeout(self, maximum: int) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RollbackReadyError(
                "SIMULATION_TIMEOUT",
                "The analysis exceeded the total sandbox runtime limit.",
                status_code=408,
                analysis_id=self.analysis_id,
                details={
                    "limit_seconds": settings.rollbackready_total_runtime_seconds,
                },
            )
        return max(1, min(maximum, math.ceil(remaining)))

    def close(self) -> None:
        if self._container:
            subprocess.run(
                ["docker", "rm", "--force", self._container],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self._container = None
        if self._data and self._binaries.get("pg_ctl"):
            subprocess.run(
                [
                    self._binaries["pg_ctl"],
                    "stop",
                    "--pgdata",
                    str(self._data),
                    "--mode=immediate",
                    "--wait",
                    "--silent",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        if self._root:
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None
        self._started = False


@contextmanager
def acquire_sandbox(analysis_id: str) -> Iterator[PostgresSandbox]:
    if not _simulation_lock.acquire(blocking=False):
        raise RollbackReadyError(
            "SIMULATOR_BUSY",
            "This instance is already running a simulation. Retry shortly.",
            status_code=409,
            analysis_id=analysis_id,
        )
    try:
        with PostgresSandbox(analysis_id) as sandbox:
            yield sandbox
    finally:
        _simulation_lock.release()


def _affected_rows(output: str) -> int | None:
    matches = re.findall(r"^(?:INSERT\s+\d+|UPDATE|DELETE|MERGE|COPY)\s+(\d+)\s*$", output, re.MULTILINE)
    return int(matches[-1]) if matches else None


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _compatibility_schema_line(line: str) -> str:
    """Ignore column defaults while retaining target schema invariants."""
    if not line.startswith("column|"):
        return line
    parts = line.split("|", 6)
    return "|".join(parts[:6])


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
