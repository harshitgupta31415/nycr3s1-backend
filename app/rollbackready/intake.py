from __future__ import annotations

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
    sql_hash,
    validate_sql_policy,
)

MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 250
MAX_COMPRESSION_RATIO = 200
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
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
    if seed is not None:
        validate_sql_policy(seed)
    legacy = _load_legacy_queries(files.get("rollbackready/legacy-queries.json"))

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
    return load_project_bundle(buffer.getvalue(), "20260809100000_add_phone")


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
