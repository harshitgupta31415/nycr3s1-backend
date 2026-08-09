from __future__ import annotations

from dataclasses import replace

import pytest

from app.core.config import settings
from app.rollbackready.contracts import GeneratedPlanPayload, PlanPhase
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.intake import load_demo_bundle
from app.rollbackready.planning import (
    RecoveryPlanner,
    _planner_prompt,
    _safe_default_for_type,
    _safe_identifier,
)
from app.rollbackready.risk import analyze_risks


def _payload() -> GeneratedPlanPayload:
    return GeneratedPlanPayload(
        strategy="Expand and contract",
        summary="Use a compatible staged migration.",
        assumptions=["Synthetic fixtures represent the risky shape."],
        phases=[
            PlanPhase(
                name="Expand",
                objective="Add the column safely.",
                sql=['ALTER TABLE "users" ADD COLUMN "phone" TEXT'],
                application_changes=["Write the new field."],
                verification_sql=[
                    'SELECT COUNT(*) FROM "users" WHERE "phone" IS NULL'
                ],
                rollback_guidance="Use a forward fix.",
            )
        ],
        limitations=["Production lock duration is not measured."],
    )


def test_deterministic_planner_generates_valid_expand_contract_plan(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.rollbackready.planning.settings",
        replace(settings, google_cloud_project=None),
    )
    findings = analyze_risks(load_demo_bundle())

    plan = RecoveryPlanner().generate("analysis-1", findings)

    assert plan.provider == "deterministic-fallback"
    assert plan.deterministic_fallback
    assert len(plan.phases) == 3
    assert all(phase.sql for phase in plan.phases)
    assert all(phase.verification_sql for phase in plan.phases)


def test_planner_uses_structured_vertex_response(monkeypatch) -> None:
    class _StructuredModel:
        def invoke(self, _: str) -> dict:
            return _payload().model_dump(mode="json")

    class _Model:
        def __init__(self, **_: object) -> None:
            pass

        def with_structured_output(self, *_: object, **__: object):
            return _StructuredModel()

    monkeypatch.setattr(
        "app.rollbackready.planning.settings",
        replace(settings, google_cloud_project="test-project"),
    )
    monkeypatch.setattr(
        "app.rollbackready.planning.ChatGoogleGenerativeAI",
        _Model,
    )

    plan = RecoveryPlanner().generate(
        "analysis-2", analyze_risks(load_demo_bundle())
    )

    assert plan.provider == "gemini"
    assert not plan.deterministic_fallback
    assert plan.model == settings.gemini_model


def test_provider_failure_falls_back_without_exposing_error(monkeypatch) -> None:
    class _FailingModel:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("provider secret details")

    monkeypatch.setattr(
        "app.rollbackready.planning.settings",
        replace(settings, google_cloud_project="test-project"),
    )
    monkeypatch.setattr(
        "app.rollbackready.planning.ChatGoogleGenerativeAI",
        _FailingModel,
    )

    plan = RecoveryPlanner().generate(
        "analysis-3", analyze_risks(load_demo_bundle())
    )

    assert plan.provider == "deterministic-fallback"
    assert "provider secret details" not in plan.summary


def test_unsupported_finding_has_no_unsafe_generic_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.rollbackready.planning.settings",
        replace(settings, google_cloud_project=None),
    )
    finding = analyze_risks(load_demo_bundle())[0].model_copy(
        update={"category": "UNKNOWN_RISK"}
    )

    with pytest.raises(RollbackReadyError) as error:
        RecoveryPlanner().generate("analysis-4", [finding])

    assert error.value.code == "PLAN_GENERATION_UNAVAILABLE"


def test_semantic_policy_rejects_empty_and_unsafe_plans() -> None:
    planner = RecoveryPlanner()
    no_migration = _payload().model_copy(deep=True)
    no_migration.phases[0].sql = []
    with pytest.raises(RollbackReadyError) as missing_sql:
        planner._validate_semantics(no_migration)
    assert missing_sql.value.code == "PLAN_REJECTED"

    no_assertion = _payload().model_copy(deep=True)
    no_assertion.phases[0].verification_sql = []
    with pytest.raises(RollbackReadyError) as missing_assertion:
        planner._validate_semantics(no_assertion)
    assert missing_assertion.value.code == "PLAN_REJECTED"

    unsafe = _payload().model_copy(deep=True)
    unsafe.phases[0].sql = ["CREATE ROLE attacker SUPERUSER"]
    with pytest.raises(RollbackReadyError):
        planner._validate_semantics(unsafe)


def test_prompt_and_identifier_helpers_keep_inputs_constrained() -> None:
    finding = analyze_risks(load_demo_bundle())[0]
    prompt = _planner_prompt([finding])

    assert finding.category in prompt
    assert _safe_identifier('"users"') == '"users"'
    with pytest.raises(RollbackReadyError):
        _safe_identifier("users; DROP TABLE users")
    assert _safe_default_for_type("TEXT") == "'unknown'"
    assert _safe_default_for_type("BOOLEAN") == "FALSE"
    assert _safe_default_for_type("INTEGER") == "0"
    assert _safe_default_for_type("TIMESTAMP") == "CURRENT_TIMESTAMP"
    assert _safe_default_for_type("JSONB") == "'unknown'"
