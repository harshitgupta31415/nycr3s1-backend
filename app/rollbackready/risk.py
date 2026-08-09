from __future__ import annotations

import re
from uuid import uuid4

from app.rollbackready.contracts import EvidenceSource, RiskFinding, Severity
from app.rollbackready.intake import ProjectBundle
from app.rollbackready.sql import PolicyStatement, redact_sql


def analyze_risks(bundle: ProjectBundle) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    for statement in bundle.candidate_statements:
        findings.extend(_statement_findings(statement))
    findings.extend(_legacy_compatibility_findings(bundle))
    return _deduplicate(findings)


def _statement_findings(statement: PolicyStatement) -> list[RiskFinding]:
    sql = statement.sql
    upper = sql.upper()
    findings: list[RiskFinding] = []

    drop_table = re.search(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)", sql, re.IGNORECASE)
    if drop_table:
        findings.append(
            _finding(
                statement,
                Severity.CRITICAL,
                "DESTRUCTIVE_DATA_LOSS",
                _identifier(drop_table.group(1)),
                "Dropping a table destroys its schema and may permanently remove all contained data.",
                "Use an expand-and-contract rollout; stop writes, archive data, and remove the table only after a measured compatibility window.",
            )
        )

    drop_column = re.search(
        r"\bALTER\s+TABLE\s+([\w.\"]+)[\s\S]*?\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?([\w\"]+)",
        sql,
        re.IGNORECASE,
    )
    if drop_column:
        findings.append(
            _finding(
                statement,
                Severity.CRITICAL,
                "DESTRUCTIVE_DATA_LOSS",
                f"{_identifier(drop_column.group(1))}.{_identifier(drop_column.group(2))}",
                "Dropping a column can permanently discard values and immediately breaks callers that still reference it.",
                "Retain the old column during a compatibility window, dual-write, backfill, then remove it in a later migration.",
            )
        )

    truncate = re.search(r"\bTRUNCATE\s+(?:TABLE\s+)?([\w.\"]+)", sql, re.IGNORECASE)
    if truncate:
        findings.append(
            _finding(
                statement,
                Severity.CRITICAL,
                "DESTRUCTIVE_DATA_LOSS",
                _identifier(truncate.group(1)),
                "TRUNCATE removes every fixture row and is not data-reversible from schema rollback alone.",
                "Remove destructive data operations from the schema migration and use an independently reviewed retention workflow.",
            )
        )

    add_required = re.search(
        r"\bALTER\s+TABLE\s+([\w.\"]+)\s+ADD\s+(?:COLUMN\s+)?([\w\"]+)\s+([\w.\"\[\]]+(?:\s*\([^)]*\))?)[\s\S]*?\bNOT\s+NULL\b",
        sql,
        re.IGNORECASE,
    )
    if add_required and not re.search(r"\bDEFAULT\b", sql, re.IGNORECASE):
        table = _identifier(add_required.group(1))
        column = _identifier(add_required.group(2))
        findings.append(
            _finding(
                statement,
                Severity.HIGH,
                "CONSTRAINT_EXISTING_DATA",
                f"{table}.{column}",
                f"Adding required column {column} without a default or backfill conflicts with existing rows and old inserts.",
                "Add the column as nullable, deploy compatible code, backfill in bounded batches, then enforce the constraint in a later phase.",
            )
        )

    set_not_null = re.search(
        r"\bALTER\s+TABLE\s+([\w.\"]+)[\s\S]*?\bALTER\s+(?:COLUMN\s+)?([\w\"]+)\s+SET\s+NOT\s+NULL",
        sql,
        re.IGNORECASE,
    )
    if set_not_null:
        findings.append(
            _finding(
                statement,
                Severity.HIGH,
                "CONSTRAINT_EXISTING_DATA",
                f"{_identifier(set_not_null.group(1))}.{_identifier(set_not_null.group(2))}",
                "A NOT NULL constraint can fail if any existing row still contains NULL.",
                "Verify and backfill NULL rows before enforcing the constraint.",
            )
        )

    unique = re.search(
        r"\b(?:CREATE\s+(?:UNIQUE\s+)?INDEX|ADD\s+(?:CONSTRAINT\s+[\w\"]+\s+)?UNIQUE)\b",
        sql,
        re.IGNORECASE,
    )
    if unique and "UNIQUE" in upper:
        findings.append(
            _finding(
                statement,
                Severity.HIGH,
                "CONSTRAINT_EXISTING_DATA",
                _first_table(sql),
                "A unique index or constraint can fail when fixture data contains duplicate keys.",
                "Run a duplicate preflight, resolve conflicts deterministically, then create the unique index.",
            )
        )

    if re.search(r"\bADD\s+(?:CONSTRAINT\s+[\w\"]+\s+)?CHECK\b", sql, re.IGNORECASE):
        findings.append(
            _finding(
                statement,
                Severity.MEDIUM,
                "CONSTRAINT_EXISTING_DATA",
                _first_table(sql),
                "A new check constraint must validate every existing row.",
                "Add the constraint as NOT VALID, repair rows, then validate it separately.",
            )
        )

    if re.search(r"\bADD\s+(?:CONSTRAINT\s+[\w\"]+\s+)?FOREIGN\s+KEY\b", sql, re.IGNORECASE):
        findings.append(
            _finding(
                statement,
                Severity.HIGH,
                "CONSTRAINT_EXISTING_DATA",
                _first_table(sql),
                "A new foreign key can fail if fixture rows reference missing parent rows.",
                "Repair orphaned rows and add the constraint NOT VALID before a separate validation step.",
            )
        )

    rename = re.search(
        r"\bALTER\s+TABLE\s+([\w.\"]+)[\s\S]*?\bRENAME\s+(?:COLUMN\s+)?([\w\"]+)\s+TO\s+([\w\"]+)",
        sql,
        re.IGNORECASE,
    )
    if rename:
        findings.append(
            _finding(
                statement,
                Severity.HIGH,
                "BACKWARD_INCOMPATIBILITY",
                f"{_identifier(rename.group(1))}.{_identifier(rename.group(2))}",
                "An immediate column rename removes the old contract used by deployed application versions.",
                "Add the new column, dual-read and dual-write, backfill, and rename or remove only after old callers are retired.",
            )
        )

    type_change = re.search(
        r"\bALTER\s+TABLE\s+([\w.\"]+)[\s\S]*?\bALTER\s+(?:COLUMN\s+)?([\w\"]+)\s+(?:SET\s+DATA\s+)?TYPE\s+([\w.\"\[\]]+(?:\s*\([^)]*\))?)",
        sql,
        re.IGNORECASE,
    )
    if type_change:
        findings.append(
            _finding(
                statement,
                Severity.HIGH,
                "TYPE_CHANGE_DATA_LOSS",
                f"{_identifier(type_change.group(1))}.{_identifier(type_change.group(2))}",
                "An in-place type change may rewrite the table, reject existing values, or perform a lossy cast.",
                "Introduce a new typed column, backfill with explicit validation, switch readers, then retire the old column.",
            )
        )

    if re.search(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", sql, re.IGNORECASE) and not re.search(
        r"\bCONCURRENTLY\b", sql, re.IGNORECASE
    ):
        findings.append(
            _finding(
                statement,
                Severity.MEDIUM,
                "LOCK_RISK_HEURISTIC",
                _first_table(sql),
                "Non-concurrent index creation can block writes on a busy production table; synthetic timing does not measure production lock duration.",
                "Consider CREATE INDEX CONCURRENTLY in a non-transactional migration and monitor it operationally.",
                source=EvidenceSource.HEURISTIC,
            )
        )
    elif type_change or re.search(r"\bSET\s+NOT\s+NULL\b|\bVALIDATE\s+CONSTRAINT\b", sql, re.IGNORECASE):
        findings.append(
            _finding(
                statement,
                Severity.MEDIUM,
                "LOCK_RISK_HEURISTIC",
                _first_table(sql),
                "This operation may scan or rewrite a production-sized table; sandbox timing is not a production lock measurement.",
                "Schedule the operation separately with lock monitoring and an explicit timeout.",
                source=EvidenceSource.HEURISTIC,
            )
        )

    return findings


def _legacy_compatibility_findings(bundle: ProjectBundle) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    required_match = re.search(
        r"\bALTER\s+TABLE\s+([\w.\"]+)\s+ADD\s+(?:COLUMN\s+)?([\w\"]+)[\s\S]*?\bNOT\s+NULL\b",
        bundle.candidate_sql,
        re.IGNORECASE,
    )
    if required_match and not re.search(r"\bDEFAULT\b", bundle.candidate_sql, re.IGNORECASE):
        table = _identifier(required_match.group(1))
        column = _identifier(required_match.group(2))
        for query in bundle.legacy_queries:
            insert = re.search(
                rf"\bINSERT\s+INTO\s+{re.escape(table)}\s*\(([^)]*)\)",
                query.sql,
                re.IGNORECASE,
            )
            if insert:
                columns = {_identifier(value.strip()) for value in insert.group(1).split(",")}
                if column not in columns:
                    findings.append(
                        RiskFinding(
                            id=str(uuid4()),
                            severity=Severity.HIGH,
                            category="BACKWARD_INCOMPATIBILITY",
                            statement_index=1,
                            statement_shape=redact_sql(query.sql),
                            affected_object=f"{table}.{column}",
                            reason=f"Legacy query {query.name} inserts into {table} without the new required {column} column.",
                            evidence_source=EvidenceSource.STATIC,
                            remediation_hint="Keep the new column nullable or provide a compatible default until all old writers are retired.",
                        )
                    )
    return findings


def confirm_findings_from_execution(
    findings: list[RiskFinding], sanitized_error: str | None
) -> list[RiskFinding]:
    if not sanitized_error:
        return findings
    error_lower = sanitized_error.lower()
    categories = {
        "CONSTRAINT_EXISTING_DATA": ("null", "duplicate", "violates", "constraint"),
        "BACKWARD_INCOMPATIBILITY": ("does not exist", "null value", "not-null"),
    }
    updated: list[RiskFinding] = []
    for finding in findings:
        markers = categories.get(finding.category, ())
        if markers and any(marker in error_lower for marker in markers):
            updated.append(
                finding.model_copy(
                    update={
                        "confirmed": True,
                        "evidence_source": EvidenceSource.FIXTURE_EXECUTION,
                    }
                )
            )
        else:
            updated.append(finding)
    return updated


def _finding(
    statement: PolicyStatement,
    severity: Severity,
    category: str,
    affected_object: str | None,
    reason: str,
    hint: str,
    *,
    source: EvidenceSource = EvidenceSource.STATIC,
) -> RiskFinding:
    return RiskFinding(
        id=str(uuid4()),
        severity=severity,
        category=category,
        statement_index=statement.index,
        statement_shape=statement.shape,
        affected_object=affected_object,
        reason=reason,
        evidence_source=source,
        remediation_hint=hint,
    )


def _identifier(value: str) -> str:
    return value.strip().strip('"').lower()


def _first_table(sql: str) -> str | None:
    match = re.search(
        r"\b(?:ALTER\s+TABLE|CREATE\s+(?:UNIQUE\s+)?INDEX[\s\S]*?\bON|TRUNCATE(?:\s+TABLE)?)\s+([\w.\"]+)",
        sql,
        re.IGNORECASE,
    )
    return _identifier(match.group(1)) if match else None


def _deduplicate(findings: list[RiskFinding]) -> list[RiskFinding]:
    seen: set[tuple[str, int | None, str | None, str]] = set()
    result: list[RiskFinding] = []
    for finding in findings:
        key = (
            finding.category,
            finding.statement_index,
            finding.affected_object,
            finding.reason,
        )
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result
