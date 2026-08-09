from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

try:
    from pg_query import parse as pg_parse
    from pg_query import walk as pg_walk
except ImportError:  # Windows has no supported pg-query-python wheel.
    pg_parse = None
    pg_walk = None

import sqlglot

from app.rollbackready.errors import RollbackReadyError

MAX_CANDIDATE_STATEMENTS = 25
MAX_LEGACY_QUERIES = 20

_LEGACY_ROOTS = {"SelectStmt", "InsertStmt", "UpdateStmt", "DeleteStmt"}
_MIGRATION_ROOTS = _LEGACY_ROOTS | {
    "AlterEnumStmt",
    "AlterTableStmt",
    "CommentStmt",
    "CreateEnumStmt",
    "CreateSchemaStmt",
    "CreateStmt",
    "DropStmt",
    "IndexStmt",
    "RenameStmt",
    "TruncateStmt",
    "ViewStmt",
}

_BLOCKED_NODE_CATEGORIES = {
    "AlterDatabaseSetStmt": "server_administration",
    "AlterDatabaseStmt": "server_administration",
    "AlterDefaultPrivilegesStmt": "privilege_administration",
    "AlterEventTrigStmt": "server_extension",
    "AlterExtensionContentsStmt": "server_extension",
    "AlterExtensionStmt": "server_extension",
    "AlterForeignServerStmt": "external_access",
    "AlterObjectDependsStmt": "server_administration",
    "AlterOwnerStmt": "privilege_administration",
    "AlterPublicationStmt": "replication",
    "AlterRoleSetStmt": "privilege_administration",
    "AlterRoleStmt": "privilege_administration",
    "AlterSubscriptionStmt": "replication",
    "AlterSystemStmt": "server_configuration",
    "CallStmt": "procedural_code",
    "CompositeTypeStmt": "procedural_code",
    "CopyStmt": "filesystem_execution",
    "CreateAmStmt": "server_extension",
    "CreateCastStmt": "server_extension",
    "CreateConversionStmt": "server_extension",
    "CreateDomainStmt": "procedural_code",
    "CreateEventTrigStmt": "server_extension",
    "CreateExtensionStmt": "server_extension",
    "CreateFdwStmt": "external_access",
    "CreateForeignServerStmt": "external_access",
    "CreateForeignTableStmt": "external_access",
    "CreateFunctionStmt": "procedural_code",
    "CreatePLangStmt": "procedural_code",
    "CreatePolicyStmt": "privilege_administration",
    "CreatePublicationStmt": "replication",
    "CreateRoleStmt": "privilege_administration",
    "CreateSubscriptionStmt": "replication",
    "CreatedbStmt": "server_administration",
    "DoStmt": "procedural_code",
    "DropRoleStmt": "privilege_administration",
    "DropSubscriptionStmt": "replication",
    "DropdbStmt": "server_administration",
    "GrantRoleStmt": "privilege_administration",
    "GrantStmt": "privilege_administration",
    "ImportForeignSchemaStmt": "external_access",
    "LoadStmt": "filesystem_execution",
    "SecLabelStmt": "server_administration",
    "TransactionStmt": "transaction_control",
    "VariableSetStmt": "server_configuration",
}

_BLOCKED_FUNCTIONS = {
    "dblink",
    "lo_export",
    "lo_import",
    "pg_backup_start",
    "pg_backup_stop",
    "pg_cancel_backend",
    "pg_create_restore_point",
    "pg_file_rename",
    "pg_file_sync",
    "pg_file_unlink",
    "pg_log_backend_memory_contexts",
    "pg_ls_dir",
    "pg_promote",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_sleep",
    "pg_stat_file",
    "pg_switch_wal",
    "pg_terminate_backend",
    "pg_write_file",
    "set_config",
}

_ROOT_KINDS = {
    "AlterEnumStmt": "ALTER",
    "AlterTableStmt": "ALTER",
    "CommentStmt": "COMMENT",
    "CreateEnumStmt": "CREATE",
    "CreateSchemaStmt": "CREATE",
    "CreateStmt": "CREATE",
    "DeleteStmt": "DELETE",
    "DropStmt": "DROP",
    "IndexStmt": "CREATE",
    "InsertStmt": "INSERT",
    "RenameStmt": "ALTER",
    "SelectStmt": "SELECT",
    "TruncateStmt": "TRUNCATE",
    "UpdateStmt": "UPDATE",
    "ViewStmt": "CREATE",
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
    if "\x00" in script:
        raise RollbackReadyError(
            "INVALID_SQL",
            "The SQL could not be parsed by the PostgreSQL 18 policy engine.",
            analysis_id=analysis_id,
        )
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

    allowed_roots = _LEGACY_ROOTS if legacy_query else _MIGRATION_ROOTS
    validated: list[PolicyStatement] = []
    for index, statement in enumerate(statements, start=1):
        shape = redact_sql(statement)
        root_name, nodes = _parse_statement_ast(
            statement,
            statement_index=index,
            analysis_id=analysis_id,
        )
        if root_name not in allowed_roots:
            raise RollbackReadyError(
                "UNSUPPORTED_SQL",
                "The SQL statement type is not supported in this analysis mode.",
                analysis_id=analysis_id,
                details={
                    "statement_index": index,
                    "category": "unsupported_statement",
                    "node_type": root_name,
                },
            )
        _validate_ast_nodes(
            nodes,
            statement_index=index,
            analysis_id=analysis_id,
        )
        kind = _ROOT_KINDS[root_name]
        validated.append(PolicyStatement(index, statement, shape, kind))
    return validated


def _parse_statement_ast(
    statement: str,
    *,
    statement_index: int,
    analysis_id: str | None,
) -> tuple[str, list[object]]:
    try:
        if pg_parse is None or pg_walk is None:
            return _parse_with_development_fallback(
                statement,
                statement_index=statement_index,
                analysis_id=analysis_id,
            )
        tree = pg_parse(statement)
        if len(tree.stmts) != 1:
            raise ValueError("expected one parsed statement")
        nodes = [node for _, node in pg_walk(tree)]
        raw_statement = tree.stmts[0]
        root = next(
            node for field, node in pg_walk(raw_statement) if field == "stmt"
        )
    except RollbackReadyError:
        raise
    except Exception as exc:
        raise RollbackReadyError(
            "INVALID_SQL",
            "The SQL could not be parsed by the PostgreSQL 18 policy engine.",
            analysis_id=analysis_id,
            details={"statement_index": statement_index},
        ) from exc
    return type(root).__name__, nodes


def _parse_with_development_fallback(
    statement: str,
    *,
    statement_index: int,
    analysis_id: str | None,
) -> tuple[str, list[object]]:
    """Conservative Windows fallback; production images use libpg_query."""
    normalized = re.sub(r"/\*.*?\*/", " ", statement, flags=re.DOTALL)
    normalized = re.sub(r"--[^\r\n]*", " ", normalized)
    normalized = re.sub(
        r'"((?:[^"]|"")*)"',
        lambda match: match.group(1).replace('""', '"'),
        normalized,
    )
    normalized = re.sub(r"\s+", " ", normalized).upper()
    blocked = re.search(
        r"\b(?:CREATE|ALTER|DROP)\s+(?:DATABASE|ROLE|USER|EXTENSION|FUNCTION|LANGUAGE|SERVER|PUBLICATION|SUBSCRIPTION)\b"
        r"|\b(?:GRANT|REVOKE|COPY|CALL|DO|LOAD|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|PREPARE|ALTER\s+SYSTEM)\b"
        r"|\b(?:PG_SLEEP|PG_READ_FILE|PG_READ_BINARY_FILE|PG_WRITE_FILE|PG_LS_DIR|PG_STAT_FILE|LO_IMPORT|LO_EXPORT|DBLINK|SET_CONFIG)\s*\(",
        normalized,
    )
    if blocked:
        _unsafe_node(
            statement_index,
            "unsafe_operation",
            "DevelopmentFallbackPolicy",
            analysis_id,
        )
    expression = sqlglot.parse_one(statement, read="postgres")
    root = {
        "alter": "AlterTableStmt",
        "comment": "CommentStmt",
        "create": "CreateStmt",
        "delete": "DeleteStmt",
        "drop": "DropStmt",
        "insert": "InsertStmt",
        "select": "SelectStmt",
        "truncate": "TruncateStmt",
        "update": "UpdateStmt",
    }.get(expression.key, type(expression).__name__)
    return root, []


def _validate_ast_nodes(
    nodes: list[object],
    *,
    statement_index: int,
    analysis_id: str | None,
) -> None:
    for node in nodes:
        node_name = type(node).__name__
        category = _BLOCKED_NODE_CATEGORIES.get(node_name)
        if category:
            _unsafe_node(statement_index, category, node_name, analysis_id)
        if node_name == "FuncCall":
            function_name = _function_name(node)
            if function_name in _BLOCKED_FUNCTIONS:
                _unsafe_node(
                    statement_index,
                    "external_access" if function_name != "pg_sleep" else "resource_exhaustion",
                    node_name,
                    analysis_id,
                )
        if node_name == "RangeVar":
            schema_name = str(getattr(node, "schemaname", "")).lower()
            if schema_name in {"information_schema", "pg_catalog", "pg_temp"}:
                _unsafe_node(
                    statement_index,
                    "system_schema_access",
                    node_name,
                    analysis_id,
                )


def _function_name(node: object) -> str:
    parts: list[str] = []
    for part in getattr(node, "funcname", ()):
        value = getattr(getattr(part, "string", None), "sval", "")
        if value:
            parts.append(str(value).lower())
    return parts[-1] if parts else ""


def _unsafe_node(
    statement_index: int,
    category: str,
    node_name: str,
    analysis_id: str | None,
) -> None:
    raise RollbackReadyError(
        "UNSUPPORTED_SQL",
        "The SQL contains a server-level or unsafe operation that cannot run in verified mode.",
        analysis_id=analysis_id,
        details={
            "statement_index": statement_index,
            "category": category,
            "node_type": node_name,
        },
    )


def sanitize_database_error(message: str | None) -> str | None:
    if not message:
        return None
    safe = redact_sql(message)
    safe = re.sub(r"(?:[A-Za-z]:\\|/)(?:[^\s:]+[/\\])+[^\s:]+", "[path]", safe)
    safe = re.sub(r"password\s*=\s*\S+", "password=[redacted]", safe, flags=re.IGNORECASE)
    return safe[:500]
