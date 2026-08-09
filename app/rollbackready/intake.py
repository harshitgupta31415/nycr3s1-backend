from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import ValidationError

from app.rollbackready.contracts import (
    ArtifactDigest,
    ArtifactManifest,
    EvidenceLevel,
    LegacyQuery,
    MigrationArtifact,
)
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.sql import (
    MAX_LEGACY_QUERIES,
    PolicyStatement,
    split_sql,
    sql_hash,
    validate_sql_policy,
)

MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 250
MAX_COMPRESSION_RATIO = 200
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_FIXTURE_ROWS = 1_000
MAX_FIXTURE_CELL_LENGTH = 2_000
NESTED_ARCHIVE_SUFFIXES = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar"}


@dataclass(frozen=True, slots=True)
class ProjectBundle:
    manifest: ArtifactManifest
    schema_prisma: str | None
    migration_lock: str | None
    prior_migrations: tuple[tuple[str, str], ...]
    candidate_sql: str
    candidate_statements: tuple[PolicyStatement, ...]
    seed_sql: str | None
    legacy_queries: tuple[LegacyQuery, ...]
    evidence_level: EvidenceLevel

    @property
    def ready_for_simulation(self) -> bool:
        return self.evidence_level is EvidenceLevel.SANDBOX_SIMULATED


@dataclass(frozen=True, slots=True)
class _BaselineColumn:
    name: str
    data_type: str
    required: bool
    generated: bool


@dataclass(frozen=True, slots=True)
class _BaselineTable:
    name: str
    columns: tuple[_BaselineColumn, ...]


def load_project_bundle(archive: bytes, candidate_migration: str) -> ProjectBundle:
    if not archive:
        raise RollbackReadyError("EMPTY_ARCHIVE", "The project bundle is empty.")
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise RollbackReadyError(
            "UPLOAD_TOO_LARGE",
            "The compressed project bundle exceeds the 10 MiB limit.",
            status_code=413,
            details={"limit_bytes": MAX_ARCHIVE_BYTES},
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", candidate_migration):
        raise RollbackReadyError(
            "INVALID_CANDIDATE",
            "The candidate migration must be a migration-folder basename.",
        )

    files = _read_safe_zip(archive)
    files = _strip_single_root(files)
    schema_path = "prisma/schema.prisma"
    lock_path = "prisma/migrations/migration_lock.toml"
    candidate_path = f"prisma/migrations/{candidate_migration}/migration.sql"

    migration_paths = sorted(
        path
        for path in files
        if re.fullmatch(r"prisma/migrations/[^/]+/migration\.sql", path)
    )
    if candidate_path not in files:
        raise RollbackReadyError(
            "CANDIDATE_NOT_FOUND",
            "The selected candidate migration was not found in the project history.",
            details={"candidate_migration": candidate_migration},
        )
    later_migrations = [
        path for path in migration_paths if _migration_folder(path) > candidate_migration
    ]
    if later_migrations:
        raise RollbackReadyError(
            "AMBIGUOUS_CANDIDATE_ORDER",
            "The selected candidate is not the final migration in the uploaded history.",
            details={"later_migration_count": len(later_migrations)},
        )

    schema = _decode_optional(files, schema_path)
    lockfile = _decode_optional(files, lock_path)
    schema_provider = _prisma_provider(schema)
    lock_provider = _lock_provider(lockfile)
    if schema_provider and lock_provider and schema_provider != lock_provider:
        raise RollbackReadyError(
            "PROVIDER_MISMATCH",
            "schema.prisma and migration_lock.toml specify different providers.",
            details={"schema_provider": schema_provider, "lock_provider": lock_provider},
        )
    provider = schema_provider or lock_provider

    candidate_sql = _decode_required(files[candidate_path], candidate_path)
    candidate_statements = tuple(validate_sql_policy(candidate_sql))
    prior: list[tuple[str, str]] = []
    migrations: list[MigrationArtifact] = []
    for path in migration_paths:
        folder = _migration_folder(path)
        sql = _decode_required(files[path], path)
        statements = validate_sql_policy(sql)
        is_candidate = folder == candidate_migration
        migrations.append(
            MigrationArtifact(
                folder=folder,
                sha256=sql_hash(sql),
                byte_count=len(files[path]),
                statement_count=len(statements),
                candidate=is_candidate,
            )
        )
        if folder < candidate_migration:
            prior.append((folder, sql))

    seed = _decode_optional(files, "rollbackready/seed.sql")
    fixture_source: str | None = None
    if seed is not None:
        validate_sql_policy(seed)
        fixture_source = "user_supplied"
    else:
        seed = _seed_from_csv(files)
        if seed is not None:
            fixture_source = "user_supplied"
        else:
            seed = _synthesize_seed(tuple(prior))
            if seed is not None:
                fixture_source = "synthesized"

    legacy_content = files.get("rollbackready/legacy-queries.json")
    legacy = _load_legacy_queries(legacy_content)
    legacy_query_source: str | None = "user_supplied" if legacy_content else None
    if not legacy:
        legacy = _synthesize_legacy_queries(tuple(prior))
        if legacy:
            legacy_query_source = "synthesized"

    has_verified_inputs = bool(
        provider == "postgresql"
        and schema is not None
        and lockfile is not None
        and seed is not None
        and legacy
    )
    evidence_level = (
        EvidenceLevel.SANDBOX_SIMULATED
        if has_verified_inputs
        else EvidenceLevel.STATIC_ANALYSIS_ONLY
    )
    manifest = ArtifactManifest(
        archive_sha256=hashlib.sha256(archive).hexdigest(),
        archive_byte_count=len(archive),
        provider=provider,
        candidate_migration=candidate_migration,
        migrations=migrations,
        artifacts=[
            ArtifactDigest(
                path=path,
                sha256=hashlib.sha256(content).hexdigest(),
                byte_count=len(content),
            )
            for path, content in sorted(files.items())
        ],
        has_schema=schema is not None,
        has_lockfile=lockfile is not None,
        has_seed=seed is not None,
        legacy_query_count=len(legacy),
        fixture_source=fixture_source,
        legacy_query_source=legacy_query_source,
    )
    return ProjectBundle(
        manifest=manifest,
        schema_prisma=schema,
        migration_lock=lockfile,
        prior_migrations=tuple(prior),
        candidate_sql=candidate_sql,
        candidate_statements=candidate_statements,
        seed_sql=seed,
        legacy_queries=legacy,
        evidence_level=evidence_level,
    )


def load_demo_bundle() -> ProjectBundle:
    archive_bytes = build_demo_archive()
    return load_project_bundle(archive_bytes, "20260809100000_add_phone")


def build_demo_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "prisma/schema.prisma",
            'datasource db {\n  provider = "postgresql"\n  url = env("DATABASE_URL")\n}\n\nmodel User {\n  id Int @id @default(autoincrement())\n  name String\n  email String @unique\n  phone String\n}\n',
        )
        archive.writestr(
            "prisma/migrations/migration_lock.toml",
            'provider = "postgresql"\n',
        )
        archive.writestr(
            "prisma/migrations/20260808090000_init/migration.sql",
            "CREATE TABLE users (id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE);",
        )
        archive.writestr(
            "prisma/migrations/20260809100000_add_phone/migration.sql",
            "ALTER TABLE users ADD COLUMN phone TEXT NOT NULL;",
        )
        archive.writestr(
            "rollbackready/seed.sql",
            "INSERT INTO users (name, email) VALUES ('Aman', 'a@example.com'), ('Bea', 'b@example.com'), ('Chen', 'c@example.com');",
        )
        archive.writestr(
            "rollbackready/legacy-queries.json",
            json.dumps(
                [
                    {
                        "name": "old-user-profile-query",
                        "sql": "SELECT id, name, email FROM users WHERE id = 1",
                        "expected_outcome": "success",
                    },
                    {
                        "name": "old-user-registration",
                        "sql": "INSERT INTO users (name, email) VALUES ('Dee', 'd@example.com')",
                        "expected_outcome": "success",
                        "expected_affected_rows": 1,
                    },
                ]
            ),
        )
    return buffer.getvalue()


def _read_safe_zip(archive_bytes: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise RollbackReadyError(
            "INVALID_ARCHIVE", "The project bundle is not a valid ZIP archive."
        ) from exc

    files: dict[str, bytes] = {}
    total = 0
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise RollbackReadyError(
            "ARCHIVE_ENTRY_LIMIT_EXCEEDED",
            "The project bundle contains too many entries.",
            details={"limit": MAX_ARCHIVE_ENTRIES},
        )

    for info in infos:
        normalized = _normalize_archive_path(info.filename)
        if normalized is None or info.is_dir():
            continue
        if normalized in files:
            raise RollbackReadyError(
                "DUPLICATE_ARCHIVE_PATH",
                "The project bundle contains duplicate normalized paths.",
                details={"path": normalized},
            )
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise RollbackReadyError(
                "ARCHIVE_SYMLINK",
                "Symbolic links are not allowed in project bundles.",
                details={"path": normalized},
            )
        if PurePosixPath(normalized).suffix.lower() in NESTED_ARCHIVE_SUFFIXES:
            raise RollbackReadyError(
                "NESTED_ARCHIVE",
                "Nested archives are not allowed in project bundles.",
                details={"path": normalized},
            )
        if info.flag_bits & 0x1:
            raise RollbackReadyError(
                "ENCRYPTED_ARCHIVE",
                "Encrypted archive entries are not supported.",
            )
        if info.file_size > MAX_ARTIFACT_BYTES:
            raise RollbackReadyError(
                "ARTIFACT_TOO_LARGE",
                "An artifact exceeds the per-file size limit.",
                details={"path": normalized, "limit_bytes": MAX_ARTIFACT_BYTES},
            )
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise RollbackReadyError(
                "ZIP_BOMB_DETECTED",
                "The uncompressed project bundle exceeds the 50 MiB limit.",
                status_code=413,
                details={"limit_bytes": MAX_UNCOMPRESSED_BYTES},
            )
        if info.file_size and (
            info.compress_size == 0 or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise RollbackReadyError(
                "ZIP_BOMB_DETECTED",
                "An archive entry has an unsafe compression ratio.",
                details={"path": normalized},
            )
        with archive.open(info) as source:
            content = source.read(MAX_ARTIFACT_BYTES + 1)
        if len(content) > MAX_ARTIFACT_BYTES or len(content) != info.file_size:
            raise RollbackReadyError(
                "ARCHIVE_SIZE_MISMATCH",
                "An archive entry did not match its declared safe size.",
                details={"path": normalized},
            )
        files[normalized] = content
    archive.close()
    return files


def _normalize_archive_path(name: str) -> str | None:
    if "\x00" in name:
        raise RollbackReadyError(
            "ZIP_TRAVERSAL", "Archive paths must not contain null bytes."
        )
    replaced = name.replace("\\", "/")
    if not replaced or replaced.startswith("/") or re.match(r"^[A-Za-z]:", replaced):
        raise RollbackReadyError(
            "ZIP_TRAVERSAL", "Archive paths must be relative and normalized."
        )
    path = PurePosixPath(replaced)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RollbackReadyError(
            "ZIP_TRAVERSAL", "Archive paths must be relative and normalized."
        )
    normalized = path.as_posix().rstrip("/")
    if normalized != replaced.rstrip("/"):
        raise RollbackReadyError(
            "ZIP_TRAVERSAL", "Archive paths must be relative and normalized."
        )
    return normalized or None


def _strip_single_root(files: dict[str, bytes]) -> dict[str, bytes]:
    expected_roots = {"prisma", "rollbackready"}
    current_roots = {path.split("/", 1)[0] for path in files}
    if current_roots & expected_roots or len(current_roots) != 1:
        return files
    prefix = next(iter(current_roots)) + "/"
    stripped = {
        path[len(prefix) :]: content
        for path, content in files.items()
        if path.startswith(prefix)
    }
    return stripped if {path.split("/", 1)[0] for path in stripped} & expected_roots else files


def _decode_required(content: bytes, path: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RollbackReadyError(
            "INVALID_ENCODING",
            "SQL and Prisma artifacts must use UTF-8 encoding.",
            details={"path": path},
        ) from exc


def _decode_optional(files: dict[str, bytes], path: str) -> str | None:
    content = files.get(path)
    return _decode_required(content, path) if content is not None else None


def _migration_folder(path: str) -> str:
    return path.split("/")[2]


def _prisma_provider(schema: str | None) -> str | None:
    if schema is None:
        return None
    datasource = re.search(r"datasource\s+\w+\s*\{([\s\S]*?)\}", schema, re.IGNORECASE)
    if not datasource:
        return None
    provider = re.search(r'\bprovider\s*=\s*"([^"]+)"', datasource.group(1), re.IGNORECASE)
    return provider.group(1).lower() if provider else None


def _lock_provider(lockfile: str | None) -> str | None:
    if lockfile is None:
        return None
    match = re.search(r'^\s*provider\s*=\s*"([^"]+)"', lockfile, re.IGNORECASE | re.MULTILINE)
    return match.group(1).lower() if match else None


def _seed_from_csv(files: dict[str, bytes]) -> str | None:
    statements: list[str] = []
    total_rows = 0
    for path, content in sorted(files.items()):
        match = re.fullmatch(r"rollbackready/fixtures/([^/]+)\.csv", path)
        if match is None:
            match = re.fullmatch(r"([^/]+)\.csv", path)
        if match is None:
            continue
        table = _quote_identifier(match.group(1), path)
        text = _decode_required(content, path)
        try:
            rows = list(csv.reader(io.StringIO(text, newline="")))
        except csv.Error as exc:
            raise RollbackReadyError(
                "INVALID_CSV_FIXTURE",
                "A CSV fixture could not be parsed.",
                details={"path": path},
            ) from exc
        if not rows or not rows[0] or any(not item.strip() for item in rows[0]):
            raise RollbackReadyError(
                "INVALID_CSV_FIXTURE",
                "CSV fixtures require a non-empty header row.",
                details={"path": path},
            )
        headers = [_quote_identifier(item.strip(), path) for item in rows[0]]
        values: list[str] = []
        for row in rows[1:]:
            total_rows += 1
            if total_rows > MAX_FIXTURE_ROWS:
                raise RollbackReadyError(
                    "FIXTURE_ROW_LIMIT_EXCEEDED",
                    f"CSV fixtures support at most {MAX_FIXTURE_ROWS} rows.",
                )
            if len(row) != len(headers):
                raise RollbackReadyError(
                    "INVALID_CSV_FIXTURE",
                    "Every CSV row must match the header column count.",
                    details={"path": path},
                )
            encoded: list[str] = []
            for cell in row:
                if len(cell) > MAX_FIXTURE_CELL_LENGTH:
                    raise RollbackReadyError(
                        "FIXTURE_CELL_TOO_LARGE",
                        "A CSV fixture cell exceeds the safe length limit.",
                        details={"path": path, "limit": MAX_FIXTURE_CELL_LENGTH},
                    )
                encoded.append("NULL" if cell == "" else _sql_string(cell))
            values.append("(" + ", ".join(encoded) + ")")
        if values:
            statements.append(
                f"INSERT INTO {table} ({', '.join(headers)}) VALUES "
                + ", ".join(values)
            )
    if not statements:
        return None
    seed = ";\n".join(statements) + ";"
    validate_sql_policy(seed)
    return seed


def _synthesize_seed(prior: tuple[tuple[str, str], ...]) -> str | None:
    statements: list[str] = []
    for table in _baseline_tables(prior):
        columns = [column for column in table.columns if not column.generated]
        if not columns:
            continue
        rows = [
            "(" + ", ".join(_synthetic_value(table.name, column, row) for column in columns) + ")"
            for row in range(1, 4)
        ]
        statements.append(
            f"INSERT INTO {table.name} "
            f"({', '.join(column.name for column in columns)}) VALUES "
            + ", ".join(rows)
        )
    if not statements:
        return None
    seed = ";\n".join(statements) + ";"
    validate_sql_policy(seed)
    return seed


def _synthesize_legacy_queries(
    prior: tuple[tuple[str, str], ...]
) -> tuple[LegacyQuery, ...]:
    queries: list[LegacyQuery] = []
    for table in _baseline_tables(prior):
        if len(queries) >= MAX_LEGACY_QUERIES:
            break
        columns = [column.name for column in table.columns]
        select_columns = ", ".join(columns) if columns else "*"
        queries.append(
            LegacyQuery(
                name=f"synthesized-select-{_plain_identifier(table.name)}",
                sql=f"SELECT {select_columns} FROM {table.name} LIMIT 1",
            )
        )
        if len(queries) >= MAX_LEGACY_QUERIES:
            break
        writable = [column for column in table.columns if not column.generated]
        if writable:
            insert_sql = (
                f"INSERT INTO {table.name} "
                f"({', '.join(column.name for column in writable)}) VALUES "
                "("
                + ", ".join(
                    _synthetic_value(table.name, column, 1001) for column in writable
                )
                + ")"
            )
        else:
            insert_sql = f"INSERT INTO {table.name} DEFAULT VALUES"
        queries.append(
            LegacyQuery(
                name=f"synthesized-insert-{_plain_identifier(table.name)}",
                sql=insert_sql,
                expected_affected_rows=1,
            )
        )
    for query in queries:
        validate_sql_policy(query.sql, legacy_query=True)
    return tuple(queries)


def _baseline_tables(prior: tuple[tuple[str, str], ...]) -> tuple[_BaselineTable, ...]:
    tables: list[_BaselineTable] = []
    for _, script in prior:
        for statement in split_sql(script):
            match = re.match(
                r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                r"(?P<table>(?:\"?[A-Za-z_][A-Za-z0-9_]*\"?\.)?"
                r"\"?[A-Za-z_][A-Za-z0-9_]*\"?)\s*\((?P<body>[\s\S]*)\)\s*$",
                statement.strip(),
                re.IGNORECASE,
            )
            if match is None:
                continue
            table_name = _safe_table_identifier(match.group("table"))
            columns: list[_BaselineColumn] = []
            for definition in _split_comma_definitions(match.group("body")):
                if re.match(
                    r"^(?:CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK|EXCLUDE)\b",
                    definition,
                    re.IGNORECASE,
                ):
                    continue
                column = re.match(
                    r"^(?P<name>\"?[A-Za-z_][A-Za-z0-9_]*\"?)\s+"
                    r"(?P<type>[A-Za-z][A-Za-z0-9_]*(?:\s+varying)?"
                    r"(?:\s*\([^)]*\))?(?:\[\])?)(?P<rest>[\s\S]*)$",
                    definition,
                    re.IGNORECASE,
                )
                if column is None:
                    continue
                data_type = column.group("type").strip()
                rest = column.group("rest")
                generated = bool(
                    re.search(r"\b(?:SERIAL|DEFAULT|GENERATED)\b", data_type + " " + rest, re.IGNORECASE)
                )
                required = bool(
                    re.search(r"\b(?:NOT\s+NULL|PRIMARY\s+KEY)\b", rest, re.IGNORECASE)
                )
                columns.append(
                    _BaselineColumn(
                        name=_quote_identifier(column.group("name").strip('"'), "migration"),
                        data_type=data_type,
                        required=required,
                        generated=generated,
                    )
                )
            if columns:
                tables.append(_BaselineTable(table_name, tuple(columns)))
    return tuple(tables)


def _split_comma_definitions(body: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quoted = False
    for index, character in enumerate(body):
        if character == '"':
            quoted = not quoted
        elif not quoted and character == "(":
            depth += 1
        elif not quoted and character == ")":
            depth = max(0, depth - 1)
        elif not quoted and depth == 0 and character == ",":
            parts.append(body[start:index].strip())
            start = index + 1
    tail = body[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _synthetic_value(table: str, column: _BaselineColumn, row: int) -> str:
    normalized = column.data_type.lower()
    if any(token in normalized for token in ("int", "numeric", "decimal", "real", "double")):
        return str(row)
    if "bool" in normalized:
        return "TRUE" if row % 2 else "FALSE"
    if "timestamp" in normalized or normalized == "date":
        return _sql_string(f"2026-01-{(row % 28) + 1:02d}")
    if "uuid" in normalized:
        return _sql_string(f"00000000-0000-4000-8000-{row:012d}")
    if "json" in normalized:
        return _sql_string("{}")
    label = f"rr_{_plain_identifier(table)}_{_plain_identifier(column.name)}_{row}"
    return _sql_string(label[:120])


def _safe_table_identifier(value: str) -> str:
    parts = [part.strip('"') for part in value.split(".")]
    if not parts or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
        raise RollbackReadyError("INVALID_IDENTIFIER", "A migration used an unsafe table identifier.")
    return ".".join(f'"{part}"' for part in parts)


def _quote_identifier(value: str, path: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RollbackReadyError(
            "INVALID_FIXTURE_IDENTIFIER",
            "Fixture table and column names must be simple SQL identifiers.",
            details={"path": path},
        )
    return f'"{value}"'


def _plain_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value.strip('"')).strip("_")[:80]


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _load_legacy_queries(content: bytes | None) -> tuple[LegacyQuery, ...]:
    if content is None:
        return ()
    try:
        parsed = json.loads(_decode_required(content, "rollbackready/legacy-queries.json"))
    except json.JSONDecodeError as exc:
        raise RollbackReadyError(
            "INVALID_LEGACY_QUERIES",
            "legacy-queries.json must contain valid JSON.",
        ) from exc
    if not isinstance(parsed, list):
        raise RollbackReadyError(
            "INVALID_LEGACY_QUERIES",
            "legacy-queries.json must contain a JSON array.",
        )
    if len(parsed) > MAX_LEGACY_QUERIES:
        raise RollbackReadyError(
            "LEGACY_QUERY_LIMIT_EXCEEDED",
            f"At most {MAX_LEGACY_QUERIES} legacy queries are supported.",
        )
    queries: list[LegacyQuery] = []
    names: set[str] = set()
    for item in parsed:
        try:
            query = LegacyQuery.model_validate(item)
        except ValidationError as exc:
            raise RollbackReadyError(
                "INVALID_LEGACY_QUERIES",
                "A legacy query does not match the required contract.",
                details={"validation_errors": len(exc.errors())},
            ) from exc
        if query.name in names:
            raise RollbackReadyError(
                "DUPLICATE_LEGACY_QUERY",
                "Legacy query names must be unique.",
                details={"name": query.name},
            )
        names.add(query.name)
        validate_sql_policy(query.sql, legacy_query=True)
        queries.append(query)
    return tuple(queries)
