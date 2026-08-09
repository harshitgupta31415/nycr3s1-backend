from __future__ import annotations

import json
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.core.config import settings
from app.rollbackready.contracts import (
    RecoveryPlan,
    RiskFinding,
    SchemaChatPayload,
    SchemaChatRequest,
    SchemaChatResponse,
)
from app.rollbackready.sql import redact_sql

SCHEMA_CHAT_PROMPT_VERSION = "rollbackready-schema-chat-v1"


class _AdvisorState(TypedDict):
    analysis_id: str
    request: SchemaChatRequest
    findings: list[RiskFinding]
    plan: RecoveryPlan | None
    payload: SchemaChatPayload | None
    used_fallback: bool


class SchemaChangeAdvisor:
    """Constrained, advisory-only conversation over sanitized analysis evidence."""

    def __init__(self) -> None:
        workflow = StateGraph(_AdvisorState)
        workflow.add_node("gemini", self._gemini_node)
        workflow.add_node("fallback", self._fallback_node)
        workflow.add_edge(START, "gemini")
        workflow.add_conditional_edges(
            "gemini",
            lambda state: "done" if state.get("payload") is not None else "fallback",
            {"done": END, "fallback": "fallback"},
        )
        workflow.add_edge("fallback", END)
        self._graph = workflow.compile()

    def reply(
        self,
        analysis_id: str,
        request: SchemaChatRequest,
        findings: list[RiskFinding],
        plan: RecoveryPlan | None,
    ) -> SchemaChatResponse:
        result = self._graph.invoke(
            {
                "analysis_id": analysis_id,
                "request": request,
                "findings": findings,
                "plan": plan,
                "payload": None,
                "used_fallback": False,
            }
        )
        payload = result.get("payload") or self._fallback_payload(findings, plan)
        used_fallback = result.get("used_fallback", False)
        return SchemaChatResponse(
            **payload.model_dump(),
            analysis_id=analysis_id,
            provider="deterministic-fallback" if used_fallback else "gemini",
            model="deterministic-v1" if used_fallback else settings.gemini_model,
            prompt_template_version=SCHEMA_CHAT_PROMPT_VERSION,
        )

    def _gemini_node(self, state: _AdvisorState) -> dict[str, object]:
        if not settings.google_cloud_project and not settings.gemini_api_key:
            return {"payload": None}
        try:
            model = ChatGoogleGenerativeAI(
                model=settings.gemini_model,
                temperature=0.1,
                retries=0,
                request_timeout=25,
                seed=7,
                google_api_key=settings.gemini_api_key,
                vertexai=settings.google_genai_use_vertexai,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
            )
            structured = model.with_structured_output(
                SchemaChatPayload.model_json_schema(), method="json_schema"
            )
            response = structured.invoke(_advisor_messages(state))
            return {"payload": SchemaChatPayload.model_validate(response)}
        except (ValidationError, ValueError, TypeError):
            return {"payload": None}
        except Exception:  # noqa: BLE001 -- provider failures must fail closed
            return {"payload": None}

    def _fallback_node(self, state: _AdvisorState) -> dict[str, object]:
        return {
            "payload": self._fallback_payload(state["findings"], state["plan"]),
            "used_fallback": True,
        }

    def _fallback_payload(
        self, findings: list[RiskFinding], plan: RecoveryPlan | None
    ) -> SchemaChatPayload:
        primary = findings[0] if findings else None
        if primary is None:
            answer = (
                "No schema risk finding is available yet. Run the deterministic analysis first, "
                "then ask about a specific finding or recommended phase."
            )
        else:
            affected = primary.affected_object or "the candidate schema change"
            answer = (
                f"The main concern is {affected}: {primary.reason} "
                f"Recommended direction: {primary.remediation_hint}"
            )
            if plan is not None:
                phases = " → ".join(phase.name for phase in plan.phases)
                answer += (
                    f" The current advisory plan uses {phases}. It remains a proposal until "
                    "RollbackReady executes it from a clean baseline and reports verified evidence."
                )
        return SchemaChatPayload(
            answer=answer,
            suggested_questions=[
                "Why does this schema change break existing rows?",
                "Which application version should deploy first?",
                "What should I verify before enforcing the constraint?",
            ],
        )


def _advisor_messages(state: _AdvisorState) -> list[SystemMessage | HumanMessage]:
    normalized_findings = [
        {
            "id": finding.id,
            "severity": finding.severity,
            "category": finding.category,
            "statement_shape": finding.statement_shape,
            "affected_object": finding.affected_object,
            "reason": finding.reason,
            "remediation_hint": finding.remediation_hint,
        }
        for finding in state["findings"]
    ]
    plan = state["plan"]
    normalized_plan = None
    if plan is not None:
        normalized_plan = {
            "strategy": plan.strategy,
            "summary": plan.summary,
            "state": plan.state,
            "phases": [
                {
                    "name": phase.name,
                    "objective": phase.objective,
                    "sql_shapes": [redact_sql(statement) for statement in phase.sql],
                    "application_changes": phase.application_changes,
                    "rollback_guidance": phase.rollback_guidance,
                }
                for phase in plan.phases
            ],
        }
    history = [
        {"role": turn.role, "content": redact_sql(turn.content)}
        for turn in state["request"].history[-6:]
    ]
    evidence = json.dumps(
        {"findings": normalized_findings, "plan": normalized_plan},
        separators=(",", ":"),
        default=str,
    )
    system = SystemMessage(
        content=(
            "You are RollbackReady's constrained schema-change advisor. Explain only the supplied "
            "sanitized findings and generated recovery plan. Never claim a migration is safe to "
            "deploy or verified unless the supplied plan state says VERIFIED_FOR_REVIEW. Never ask "
            "for production credentials, fixture rows, or raw uploaded artifacts. Do not invent "
            "database facts. Distinguish deterministic evidence from heuristic lock risk and advisory "
            "guidance. Keep the answer practical and under 220 words. Return structured JSON. "
            f"Sanitized evidence: {evidence}"
        )
    )
    user = HumanMessage(
        content=json.dumps(
            {
                "history": history,
                "question": redact_sql(state["request"].message),
            },
            separators=(",", ":"),
        )
    )
    return [system, user]
