from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TypedDict
from uuid import uuid4

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.core.config import settings
from app.rollbackready.contracts import (
    GeneratedPlanPayload,
    PlanPhase,
    PlanState,
    RecoveryPlan,
    RiskFinding,
)
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.sql import statement_kind, validate_sql_policy

PROMPT_TEMPLATE_VERSION = "rollbackready-plan-v1"


class _PlannerState(TypedDict):
    findings: list[RiskFinding]
    payload: GeneratedPlanPayload | None
    used_fallback: bool


class RecoveryPlanner:
    def __init__(self) -> None:
        workflow = StateGraph(_PlannerState)
        workflow.add_node("gemini", self._gemini_node)
        workflow.add_node("fallback", self._fallback_node)
        workflow.add_node("policy", self._policy_node)
        workflow.add_edge(START, "gemini")
        workflow.add_conditional_edges(
            "gemini",
            lambda state: "policy" if state.get("payload") is not None else "fallback",
            {"policy": "policy", "fallback": "fallback"},
        )
        workflow.add_edge("fallback", "policy")
        workflow.add_edge("policy", END)
        self._graph = workflow.compile()

    def generate(
        self,
        analysis_id: str,
        findings: list[RiskFinding],
    ) -> RecoveryPlan:
        result = self._graph.invoke(
            {"findings": findings, "payload": None, "used_fallback": False}
        )
        payload = result.get("payload")
        if payload is None:
            raise RollbackReadyError(
                "PLAN_GENERATION_UNAVAILABLE",
                "The recovery planning graph did not produce a valid plan.",
                status_code=422,
            )
        used_fallback = result.get("used_fallback", False)
        return RecoveryPlan(
            **payload.model_dump(),
            id=str(uuid4()),
            analysis_id=analysis_id,
            state=PlanState.UNVERIFIED_CANDIDATE,
            provider="gemini" if not used_fallback else "deterministic-fallback",
            model=settings.gemini_model if not used_fallback else "deterministic-v1",
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            generated_at=datetime.now(UTC),
            deterministic_fallback=used_fallback,
        )

    def _gemini_node(self, state: _PlannerState) -> dict[str, GeneratedPlanPayload | None]:
        if not settings.google_cloud_project:
            return {"payload": None}
        try:
            model = ChatGoogleGenerativeAI(
                model=settings.gemini_model,
                temperature=0.1,
                retries=0,
                request_timeout=25,
                seed=7,
                vertexai=settings.google_genai_use_vertexai,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
            )
            structured = model.with_structured_output(
                GeneratedPlanPayload.model_json_schema(), method="json_schema"
            )
            response = structured.invoke(_planner_prompt(state["findings"]))
            return {"payload": GeneratedPlanPayload.model_validate(response)}
        except (ValidationError, ValueError, TypeError):
            return {"payload": None}
        except Exception:  # noqa: BLE001 -- provider failures must fail closed
            # Provider timeouts and availability never bypass deterministic
            # validation. The user receives a safe fallback, not provider details.
            return {"payload": None}

    def _fallback_node(self, state: _PlannerState) -> dict[str, object]:
        return {
            "payload": self._deterministic_fallback(state["findings"]),
            "used_fallback": True,
        }

    def _policy_node(self, state: _PlannerState) -> dict[str, GeneratedPlanPayload]:
        payload = state.get("payload")
        if payload is None:
            raise RollbackReadyError(
                "PLAN_REJECTED", "The planning graph produced no plan.", status_code=422
            )
        self._validate_semantics(payload)
        return {"payload": payload}

    def _deterministic_fallback(
        self, findings: list[RiskFinding]
    ) -> GeneratedPlanPayload:
        required = next(
            (
                finding
                for finding in findings
                if finding.category == "CONSTRAINT_EXISTING_DATA"
                and finding.affected_object
                and "Adding required column" in finding.reason
                and finding.statement_shape
            ),
            None,
        )
        if required:
            match = re.search(
                r"ALTER\s+TABLE\s+([\w.\"]+)\s+ADD\s+(?:COLUMN\s+)?([\w\"]+)\s+([\w.\"\[\]]+(?:\s*\([^)]*\))?)",
                required.statement_shape,
                re.IGNORECASE,
            )
            if match:
                table = _safe_identifier(match.group(1))
                column = _safe_identifier(match.group(2))
                display_table = table.strip('"')
                display_column = column.strip('"')
                data_type = match.group(3).strip()
                default = _safe_default_for_type(data_type)
                return GeneratedPlanPayload(
                    strategy="Expand, backfill, and contract with an old-writer default",
                    summary=(
                        f"Introduce {display_table}.{display_column} without an immediate constraint, backfill existing rows, "
                        "then retain a compatible default before enforcing NOT NULL."
                    ),
                    assumptions=[
                        "The placeholder value is acceptable only as a transitional synthetic default.",
                        "Application writers can be updated before the default is removed.",
                    ],
                    phases=[
                        PlanPhase(
                            name="Expand",
                            objective="Add the new column without invalidating existing rows or old writers.",
                            sql=[f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {data_type}"],
                            application_changes=[
                                f"Deploy readers that tolerate NULL {display_column} values and writers that populate the column."
                            ],
                            verification_sql=[
                                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL"
                            ],
                            rollback_guidance=f"Drop {display_column} only before new writes depend on it; otherwise use a forward fix.",
                        ),
                        PlanPhase(
                            name="Backfill",
                            objective="Populate every pre-existing row before constraint enforcement.",
                            sql=[
                                f"UPDATE {table} SET {column} = {default} WHERE {column} IS NULL"
                            ],
                            application_changes=[
                                "Monitor the bounded backfill and confirm the null count reaches zero."
                            ],
                            verification_sql=[
                                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL"
                            ],
                            rollback_guidance="Stop the backfill and correct values with a forward data repair; do not discard populated values.",
                        ),
                        PlanPhase(
                            name="Contract",
                            objective="Preserve old-writer compatibility while enforcing the target constraint.",
                            sql=[
                                f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {default}",
                                f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL",
                            ],
                            application_changes=[
                                "Remove the transitional default only after every old writer is retired."
                            ],
                            verification_sql=[
                                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL"
                            ],
                            rollback_guidance="Drop the NOT NULL constraint as a forward fix if an old writer remains incompatible.",
                        ),
                    ],
                    limitations=[
                        "Synthetic data cannot validate the business meaning of the placeholder value.",
                        "Production lock duration is not measured.",
                    ],
                )

        raise RollbackReadyError(
            "PLAN_GENERATION_UNAVAILABLE",
            "No deterministic recovery template supports these findings, and Gemini did not return a valid plan.",
            status_code=422,
        )

    def _validate_semantics(self, payload: GeneratedPlanPayload) -> None:
        migration_statement_count = 0
        verification_statement_count = 0
        for phase_index, phase in enumerate(payload.phases, start=1):
            for statement in phase.sql:
                validated = validate_sql_policy(statement)
                migration_statement_count += len(validated)
            for verification in phase.verification_sql:
                statements = validate_sql_policy(verification, legacy_query=True)
                verification_statement_count += len(statements)
                if any(statement_kind(item.sql) != "SELECT" for item in statements):
                    raise RollbackReadyError(
                        "PLAN_REJECTED",
                        "Plan verification SQL must be read-only SELECT statements.",
                        status_code=422,
                        details={"phase_index": phase_index},
                    )
        if migration_statement_count == 0:
            raise RollbackReadyError(
                "PLAN_REJECTED",
                "The generated plan does not contain executable migration SQL.",
                status_code=422,
            )
        if verification_statement_count == 0:
            raise RollbackReadyError(
                "PLAN_REJECTED",
                "The generated plan does not contain a read-only verification assertion.",
                status_code=422,
            )


def _planner_prompt(findings: list[RiskFinding]) -> str:
    normalized = [
        {
            "severity": finding.severity,
            "category": finding.category,
            "statement_index": finding.statement_index,
            "statement_shape": finding.statement_shape,
            "affected_object": finding.affected_object,
            "reason": finding.reason,
            "remediation_hint": finding.remediation_hint,
        }
        for finding in findings
    ]
    return (
        "You are RollbackReady's constrained PostgreSQL migration planner. "
        "Return only JSON matching the supplied schema. Produce a conservative expand-and-contract plan. "
        "Never claim that a migration is safe to deploy. Do not use server administration, filesystem, "
        "network, extension, role, database, or procedural SQL. The input contains normalized SQL shapes "
        "Prefer retryable, idempotent migration statements such as guarded object creation. "
        "with literals redacted; never invent or request fixture values. Every verification_sql entry must be "
        "a read-only SELECT assertion that returns one scalar: zero for no violations or true for success. Findings:\n"
        + json.dumps(normalized, separators=(",", ":"), default=str)
    )


def _safe_identifier(value: str) -> str:
    normalized = value.strip().strip('"')
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
        raise RollbackReadyError(
            "PLAN_REJECTED", "A generated plan used an unsafe identifier.", status_code=422
        )
    return '"' + normalized.replace('"', '""') + '"'


def _safe_default_for_type(data_type: str) -> str:
    normalized = data_type.lower()
    if any(token in normalized for token in ("char", "text", "citext")):
        return "'unknown'"
    if "bool" in normalized:
        return "FALSE"
    if any(token in normalized for token in ("int", "numeric", "decimal", "real", "double")):
        return "0"
    if "timestamp" in normalized or normalized == "date":
        return "CURRENT_TIMESTAMP"
    return "'unknown'"
