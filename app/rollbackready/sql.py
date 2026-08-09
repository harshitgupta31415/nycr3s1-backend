from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.rollbackready.errors import RollbackReadyError

MAX_CANDIDATE_STATEMENTS = 25
MAX_LEGACY_QUERIES = 20

_BLOCKED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:CREATE|DROP|ALTER)\s+(?:DATABASE|ROLE|USER|TABLESPACE)\b", "server_administration"),
    (r"\b(?:GRANT|REVOKE)\b", "privilege_administration"),
    (r"\bCOPY\b[\s\S]*?\bPROGRAM\b", "filesystem_execution"),
    (r"\b(?:CREATE|ALTER)\s+(?:EVENT\s+TRIGGER|EXTENSION)\b", "server_extension"),
    (r"\bCREATE\s+(?:SERVER|FOREIGN\s+DATA\s+WRAPPER|LANGUAGE)\b", "external_access"),
    (r"\b(?:CREATE|ALTER)\s+(?:PUBLICATION|SUBSCRIPTION)\b", "replication"),
    (r"\b(?:ALTER\s+SYSTEM|LOAD\s+|SET\s+(?:ROLE|SESSION\s+AUTHORIZATION))\b", "server_configuration"),
    (r"\b(?:pg_read_file|pg_write_file|pg_ls_dir|lo_import|lo_export|dblink|postgres_fdw)\s*\(", "external_access"),
    (r"\bpg_sleep\s*\(", "resource_exhaustion"),
    (r"\bDO\s+\$|\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b", "procedural_code"),
)

_LEGACY_ALLOWED = {"SELECT", "INSERT", "UPDATE", "DELETE"}
_MIGRATION_ALLOWED = {
    "ALTER",
    "CREATE",
    "DROP",
    "TRUNCATE",
    "INSERT",
    "UPDATE",
    "DELETE",
    "SELECT",
    "COMMENT",
    "BEGIN",
    "COMMIT",
}


@dataclass(frozen=True, slots=True)
class PolicyStatement:
    index: int
    sql: str
    shape: str
    kind: str


def split_sql(script: str) -> list[str]:
    """Split PostgreSQL SQL at top-level semicolons without executing a parser."""
    statements: list[str] = []
    start = 0
    index = 0
    single = False
    double = False
    line_comment = False
    block_depth = 0
    dollar_tag: str | None = None

    while index < len(script):
        char = script[index]
        pair = script[index : index + 2]

        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_depth:
            if pair == "/*":
                block_depth += 1
                index += 2
            elif pair == "*/":
                block_depth -= 1
                index += 2
            else:
                index += 1
            continue
        if dollar_tag is not None:
            if script.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
            else:
                index += 1
            continue
        if single:
            if char == "'" and index + 1 < len(script) and script[index + 1] == "'":
                index += 2
            elif char == "'":
                single = False
                index += 1
            else:
                index += 1
            continue
        if double:
            if char == '"' and index + 1 < len(script) and script[index + 1] == '"':
                index += 2
            elif char == '"':
                double = False
                index += 1
            else:
                index += 1
            continue

        if pair == "--":
            line_comment = True
            index += 2
        elif pair == "/*":
            block_depth = 1
            index += 2
        elif char == "'":
            single = True
            index += 1
        elif char == '"':
            double = True
            index += 1
        elif char == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", script[index:])
            if match:
                dollar_tag = match.group(0)
                index += len(dollar_tag)
            else:
                index += 1
        elif char == ";":
            statement = script[start:index].strip()
            if _has_executable_sql(statement):
                statements.append(statement)
            start = index + 1
            index += 1
        else:
            index += 1

    tail = script[start:].strip()
    if _has_executable_sql(tail):
        statements.append(tail)
    return statements


def _has_executable_sql(sql: str) -> bool:
    without_comments = re.sub(r"--[^\r\n]*|/\*[\s\S]*?\*/", "", sql)
    return bool(without_comments.strip())


def redact_sql(sql: str) -> str:
    redacted = re.sub(
        r"\$([A-Za-z_][A-Za-z0-9_]*)\$[\s\S]*?\$\1\$",
        "?",
        sql,
    )
    redacted = re.sub(r"\$\$[\s\S]*?\$\$", "?", redacted)
    redacted = re.sub(r"'(?:''|[^'])*'", "?", redacted)
    redacted = re.sub(r"\b\d+(?:\.\d+)?\b", "?", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    return redacted[:1000]


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def statement_kind(sql: str) -> str:
    cleaned = re.sub(r"\A(?:\s|--[^\r\n]*(?:\r?\n|$)|/\*[\s\S]*?\*/)*", "", sql)
    match = re.match(r"([A-Za-z]+)", cleaned)
    return match.group(1).upper() if match else "UNKNOWN"


def validate_sql_policy(
    script: str,
    *,
    legacy_query: bool = False,
    analysis_id: str | None = None,
) -> list[PolicyStatement]:
    statements = split_sql(script)
    maximum = 1 if legacy_query else MAX_CANDIDATE_STATEMENTS
    if not statements:
        raise RollbackReadyError(
            "EMPTY_SQL",
            "The SQL artifact does not contain an executable statement.",
            analysis_id=analysis_id,
        )
    if len(statements) > maximum:
        raise RollbackReadyError(
            "STATEMENT_LIMIT_EXCEEDED",
            f"The SQL artifact exceeds the {maximum}-statement limit.",
            analysis_id=analysis_id,
            details={"statement_count": len(statements), "limit": maximum},
        )

    allowed = _LEGACY_ALLOWED if legacy_query else _MIGRATION_ALLOWED
    validated: list[PolicyStatement] = []
    for index, statement in enumerate(statements, start=1):
        shape = redact_sql(statement)
        for pattern, category in _BLOCKED_PATTERNS:
            if re.search(pattern, statement, flags=re.IGNORECASE):
                raise RollbackReadyError(
                    "UNSUPPORTED_SQL",
                    "The SQL contains a server-level or unsafe operation that cannot run in verified mode.",
                    analysis_id=analysis_id,
                    details={"statement_index": index, "category": category},
                )
        kind = statement_kind(statement)
        if kind not in allowed:
            raise RollbackReadyError(
                "UNSUPPORTED_SQL",
                f"{kind.title()} statements are not supported in this analysis mode.",
                analysis_id=analysis_id,
                details={"statement_index": index, "category": "unsupported_statement"},
            )
        if legacy_query and kind in {"BEGIN", "COMMIT"}:
            raise RollbackReadyError(
                "UNSUPPORTED_SQL",
                "Legacy queries cannot contain transaction-control statements.",
                analysis_id=analysis_id,
                details={"statement_index": index, "category": "transaction_control"},
            )
        validated.append(PolicyStatement(index, statement, shape, kind))
    return validated


def sanitize_database_error(message: str | None) -> str | None:
    if not message:
        return None
    safe = redact_sql(message)
    safe = re.sub(r"(?:[A-Za-z]:\\|/)(?:[^\s:]+[/\\])+[^\s:]+", "[path]", safe)
    safe = re.sub(r"password\s*=\s*\S+", "password=[redacted]", safe, flags=re.IGNORECASE)
    return safe[:500]
