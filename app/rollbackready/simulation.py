from __future__ import annotations

import re
import time
from dataclasses import dataclass
from uuid import uuid4

from app.rollbackready.contracts import (
    EvidenceDimension,
    EvidenceSource,
    EvidenceStatus,
    LegacyQueryResult,
    RecoveryAssessment,
    RecoveryClassification,
    RetryOutcome,
    RiskFinding,
    SimulationRun,
    SnapshotSummary,
    StatementExecution,
)
from app.rollbackready.intake import ProjectBundle
from app.rollbackready.sandbox import (
    DatabaseExecution,
    PostgresSandbox,
    ScriptExecution,
    acquire_sandbox,
)
from app.rollbackready.sql import redact_sql, sql_hash, statement_kind


@dataclass(frozen=True, slots=True)
class SimulationOutcome:
    dimensions: list[EvidenceDimension]
    runs: list[SimulationRun]
    legacy_results: list[LegacyQueryResult]
    candidate_error: str | None


class SimulationEngine:
    def run(self, analysis_id: str, bundle: ProjectBundle, findings: list[RiskFinding]) -> SimulationOutcome:
        dimensions = empty_dimensions()
        runs: list[SimulationRun] = []
        legacy_results: list[LegacyQueryResult] = []
        candidate_error: str | None = None

        if not bundle.ready_for_simulation:
            return SimulationOutcome(dimensions, runs, legacy_results, None)
        if bundle.manifest.provider != "postgresql":
            return SimulationOutcome(dimensions, runs, legacy_results, None)

        with acquire_sandbox(analysis_id) as sandbox:
            setup_error = self._restore_baseline(sandbox, bundle, include_seed=False)
            if setup_error:
                _set_dimension(
                    dimensions,
                    "prior_migrations",
                    EvidenceStatus.FAIL,
                    EvidenceSource.FIXTURE_EXECUTION,
                    f"Prior migration replay failed: {setup_error}",
                )
                return SimulationOutcome(dimensions, runs, legacy_results, setup_error)
            _set_dimension(
                dimensions,
                "prior_migrations",
                EvidenceStatus.PASS,
                EvidenceSource.FIXTURE_EXECUTION,
                f"Replayed {len(bundle.prior_migrations)} prior migrations on a clean PostgreSQL 18 database.",
            )

            seed_result = sandbox.execute(bundle.seed_sql or "SELECT 1")
            if not seed_result.succeeded:
                _set_dimension(
                    dimensions,
                    "fixture_load",
                    EvidenceStatus.FAIL,
                    EvidenceSource.FIXTURE_EXECUTION,
                    f"Synthetic fixture loading failed: {seed_result.sanitized_error}",
                )
                return SimulationOutcome(
                    dimensions, runs, legacy_results, seed_result.sanitized_error
                )
            _set_dimension(
                dimensions,
                "fixture_load",
                EvidenceStatus.PASS,
                EvidenceSource.FIXTURE_EXECUTION,
                "Synthetic fixtures loaded successfully; fixture values were not persisted.",
            )
            baseline = sandbox.snapshot()

            normal_started = time.monotonic()
            candidate_script = sandbox.execute_statements(
                [statement.sql for statement in bundle.candidate_statements]
            )
            candidate_result = candidate_script.execution
            candidate_error = candidate_result.sanitized_error
            normal_snapshot = sandbox.snapshot(
                content_columns=baseline.content_columns
            )
            normal_run = SimulationRun(
                id=str(uuid4()),
                run_type="NORMAL_APPLICATION",
                status=(
                    EvidenceStatus.PASS
                    if candidate_result.succeeded
                    else EvidenceStatus.FAIL
                ),
                duration_ms=int((time.monotonic() - normal_started) * 1000),
                statements=_statement_executions(bundle, candidate_script),
                snapshot=normal_snapshot,
                sanitized_error=candidate_result.sanitized_error,
            )
            runs.append(normal_run)
            _set_dimension(
                dimensions,
                "schema_application",
                normal_run.status,
                EvidenceSource.FIXTURE_EXECUTION,
                (
                    "The candidate migration applied successfully."
                    if candidate_result.succeeded
                    else f"The candidate migration failed against production-shaped fixtures: {candidate_result.sanitized_error}"
                ),
            )

            if candidate_result.succeeded:
                data_preserved = all(
                    normal_snapshot.row_counts.get(table) == count
                    for table, count in baseline.row_counts.items()
                ) and all(
                    normal_snapshot.content_hashes.get(table) == content_hash
                    for table, content_hash in baseline.content_hashes.items()
                )
                _set_dimension(
                    dimensions,
                    "data_preservation",
                    EvidenceStatus.PASS if data_preserved else EvidenceStatus.FAIL,
                    EvidenceSource.FIXTURE_EXECUTION,
                    (
                    "Fixture row counts were preserved across every baseline table."
                    if data_preserved
                    else "One or more baseline tables changed row counts or baseline-column content during the candidate migration."
                    ),
                )
                legacy_results = self._run_legacy_queries(sandbox, bundle)
                legacy_passed = all(
                    result.status is EvidenceStatus.PASS for result in legacy_results
                )
                _set_dimension(
                    dimensions,
                    "legacy_queries",
                    EvidenceStatus.PASS if legacy_passed else EvidenceStatus.FAIL,
                    EvidenceSource.LEGACY_REPLAY,
                    (
                        f"All {len(legacy_results)} legacy queries matched their expected outcomes."
                        if legacy_passed
                        else "At least one legacy query failed or produced an unexpected result."
                    ),
                )
                rollback_run = self._rollback_verification_run(sandbox, bundle)
                runs.append(rollback_run)
                rollback_status = rollback_run.status
                rollback_summary = (
                    rollback_run.recovery.summary
                    if rollback_run.recovery
                    else "Rollback recovery could not be classified."
                )
                _set_dimension(
                    dimensions,
                    "rollback_recovery",
                    rollback_status,
                    EvidenceSource.FIXTURE_EXECUTION,
                    rollback_summary,
                )
                failure_runs = self._failure_injection_runs(
                    sandbox,
                    bundle,
                    findings,
                    normal_snapshot,
                    rollback_run.recovery,
                )
                runs.extend(failure_runs)
                recovery_passed = all(
                    run.status is EvidenceStatus.PASS for run in failure_runs
                )
                retry_passed = all(
                    run.recovery
                    and run.recovery.retry_outcome
                    in {RetryOutcome.SUCCESSFUL, RetryOutcome.IDEMPOTENT}
                    for run in failure_runs
                )
                _set_dimension(
                    dimensions,
                    "failure_recovery",
                    EvidenceStatus.PASS if recovery_passed else EvidenceStatus.FAIL,
                    EvidenceSource.FAILURE_INJECTION,
                    (
                        f"All {len(failure_runs)} interruption boundaries recovered to the target schema."
                        if recovery_passed
                        else "At least one interruption boundary could not recover to the target schema."
                    ),
                )
                _set_dimension(
                    dimensions,
                    "idempotent_retry",
                    EvidenceStatus.PASS if retry_passed else EvidenceStatus.FAIL,
                    EvidenceSource.FAILURE_INJECTION,
                    (
                        "Retries completed successfully at every supported interruption boundary."
                        if retry_passed
                        else "At least one retry produced a duplicate or conflicting state."
                    ),
                )
            else:
                _set_dimension(
                    dimensions,
                    "data_preservation",
                    EvidenceStatus.FAIL,
                    EvidenceSource.FIXTURE_EXECUTION,
                    "Data preservation cannot pass because normal migration application failed.",
                )
                _set_dimension(
                    dimensions,
                    "legacy_queries",
                    EvidenceStatus.NOT_TESTED,
                    EvidenceSource.LEGACY_REPLAY,
                    "Legacy queries were not replayed because the candidate migration did not apply.",
                )
                _set_dimension(
                    dimensions,
                    "rollback_recovery",
                    EvidenceStatus.NOT_TESTED,
                    EvidenceSource.FIXTURE_EXECUTION,
                    "Rollback was not tested because the candidate migration did not apply.",
                )
                # A boundary-zero retry supplies deterministic recovery evidence even
                # when no valid target schema can be established.
                failure_run = self._failed_candidate_retry(sandbox, bundle, findings)
                runs.append(failure_run)
                _set_dimension(
                    dimensions,
                    "failure_recovery",
                    failure_run.status,
                    EvidenceSource.FAILURE_INJECTION,
                    "The failed candidate was retried from a clean baseline; it remained unsafe."
                    if failure_run.status is EvidenceStatus.FAIL
                    else "The candidate recovered when retried from a clean baseline.",
                )
                _set_dimension(
                    dimensions,
                    "idempotent_retry",
                    failure_run.status,
                    EvidenceSource.FAILURE_INJECTION,
                    "The clean retry reproduced the migration failure."
                    if failure_run.status is EvidenceStatus.FAIL
                    else "The clean retry succeeded.",
                )

        return SimulationOutcome(dimensions, runs, legacy_results, candidate_error)

    def verify_plan(
        self,
        analysis_id: str,
        bundle: ProjectBundle,
        plan_sql: list[str],
        verification_sql: list[str] | None = None,
    ) -> SimulationOutcome:
        dimensions = empty_dimensions()
        runs: list[SimulationRun] = []
        legacy_results: list[LegacyQueryResult] = []
        verification_sql = verification_sql or []
        with acquire_sandbox(analysis_id) as sandbox:
            target_setup_error = self._restore_baseline(
                sandbox, bundle, include_seed=False
            )
            if target_setup_error:
                _set_dimension(dimensions, "prior_migrations", EvidenceStatus.FAIL, EvidenceSource.PLAN_VERIFICATION, target_setup_error)
                return SimulationOutcome(dimensions, runs, legacy_results, target_setup_error)
            target_execution = sandbox.execute(bundle.candidate_sql)
            if not target_execution.succeeded:
                _set_dimension(dimensions, "prior_migrations", EvidenceStatus.PASS, EvidenceSource.PLAN_VERIFICATION, "Fresh baseline replayed successfully.")
                _set_dimension(dimensions, "schema_application", EvidenceStatus.FAIL, EvidenceSource.PLAN_VERIFICATION, "The intended candidate target schema could not be established on an empty baseline.")
                return SimulationOutcome(
                    dimensions,
                    runs,
                    legacy_results,
                    target_execution.sanitized_error,
                )
            candidate_target = sandbox.snapshot()

            setup_error = self._restore_baseline(sandbox, bundle, include_seed=True)
            if setup_error:
                _set_dimension(dimensions, "prior_migrations", EvidenceStatus.FAIL, EvidenceSource.PLAN_VERIFICATION, setup_error)
                return SimulationOutcome(dimensions, runs, legacy_results, setup_error)
            _set_dimension(dimensions, "prior_migrations", EvidenceStatus.PASS, EvidenceSource.PLAN_VERIFICATION, "Fresh baseline replayed successfully.")
            _set_dimension(dimensions, "fixture_load", EvidenceStatus.PASS, EvidenceSource.PLAN_VERIFICATION, "Synthetic fixtures loaded successfully.")
            before = sandbox.snapshot()
            started = time.monotonic()
            plan_script = sandbox.execute_statements(plan_sql)
            execution = plan_script.execution
            after = sandbox.snapshot(content_columns=before.content_columns)
            schema_matches_target = (
                execution.succeeded
                and after.compatibility_schema_hash
                == candidate_target.compatibility_schema_hash
            )
            assertion_results: list[LegacyQueryResult] = []
            assertions_pass = True
            if execution.succeeded:
                for index, statement in enumerate(verification_sql, start=1):
                    assertion = sandbox.execute(statement, wrap_rollback=True)
                    assertion_pass = _verification_assertion_passed(assertion)
                    assertions_pass = assertions_pass and assertion_pass
                    assertion_results.append(
                        LegacyQueryResult(
                            name=f"plan-verification-{index}",
                            query_hash=sql_hash(statement),
                            status=(
                                EvidenceStatus.PASS
                                if assertion_pass
                                else EvidenceStatus.FAIL
                            ),
                            duration_ms=assertion.duration_ms,
                            affected_rows=assertion.affected_rows,
                            sanitized_error=(
                                assertion.sanitized_error
                                if assertion.sanitized_error
                                else None
                                if assertion_pass
                                else "Verification assertion reported outstanding violations."
                            ),
                        )
                    )
            schema_verified = (
                execution.succeeded
                and schema_matches_target
                and assertions_pass
            )
            run = SimulationRun(
                id=str(uuid4()),
                run_type="PLAN_VERIFICATION",
                status=EvidenceStatus.PASS if schema_verified else EvidenceStatus.FAIL,
                duration_ms=int((time.monotonic() - started) * 1000),
                statements=_sql_statement_executions(plan_sql, plan_script),
                snapshot=after,
                sanitized_error=execution.sanitized_error,
            )
            runs.append(run)
            _set_dimension(
                dimensions,
                "schema_application",
                run.status,
                EvidenceSource.PLAN_VERIFICATION,
                (
                    f"Every generated phase reached the candidate target schema and {len(assertion_results)} verification assertions passed."
                    if schema_verified
                    else f"Generated plan execution failed: {execution.sanitized_error}"
                    if not execution.succeeded
                    else "Generated SQL did not match the candidate target schema or a verification assertion failed."
                ),
            )
            if execution.succeeded:
                preserved = all(
                    after.row_counts.get(table) == count
                    for table, count in before.row_counts.items()
                ) and all(
                    after.content_hashes.get(table) == content_hash
                    for table, content_hash in before.content_hashes.items()
                )
                _set_dimension(dimensions, "data_preservation", EvidenceStatus.PASS if preserved else EvidenceStatus.FAIL, EvidenceSource.PLAN_VERIFICATION, "Baseline fixture row counts and baseline-column content hashes were preserved." if preserved else "The generated plan changed baseline fixture rows or values.")
                legacy_results = self._run_legacy_queries(sandbox, bundle)
                legacy_results.extend(assertion_results)
                legacy_pass = all(result.status is EvidenceStatus.PASS for result in legacy_results)
                _set_dimension(dimensions, "legacy_queries", EvidenceStatus.PASS if legacy_pass else EvidenceStatus.FAIL, EvidenceSource.PLAN_VERIFICATION, "Legacy query replay passed after the generated plan." if legacy_pass else "A legacy query failed after the generated plan.")
                if schema_verified and preserved and legacy_pass:
                    failure_runs = self._plan_failure_injection_runs(
                        sandbox,
                        bundle,
                        plan_sql,
                        after,
                        before.content_columns,
                    )
                    runs.extend(failure_runs)
                    recovery_passed = all(
                        run.status is EvidenceStatus.PASS for run in failure_runs
                    )
                    retry_passed = all(
                        run.recovery
                        and run.recovery.retry_outcome
                        in {RetryOutcome.SUCCESSFUL, RetryOutcome.IDEMPOTENT}
                        for run in failure_runs
                    )
                    _set_dimension(dimensions, "failure_recovery", EvidenceStatus.PASS if recovery_passed else EvidenceStatus.FAIL, EvidenceSource.PLAN_VERIFICATION, f"All {len(failure_runs)} generated-plan interruption boundaries recovered to the verified target." if recovery_passed else "At least one generated-plan interruption boundary failed recovery.")
                    _set_dimension(dimensions, "idempotent_retry", EvidenceStatus.PASS if retry_passed else EvidenceStatus.FAIL, EvidenceSource.PLAN_VERIFICATION, "Generated-plan retry reached the verified target at every boundary." if retry_passed else "At least one generated-plan retry was not idempotent.")
                else:
                    for key in ("failure_recovery", "idempotent_retry"):
                        _set_dimension(dimensions, key, EvidenceStatus.FAIL, EvidenceSource.PLAN_VERIFICATION, "Not proven because target schema, content, assertions, or legacy compatibility failed.")
            else:
                for key in ("data_preservation", "legacy_queries"):
                    _set_dimension(dimensions, key, EvidenceStatus.FAIL if key == "data_preservation" else EvidenceStatus.NOT_TESTED, EvidenceSource.PLAN_VERIFICATION, "Not proven because generated SQL failed.")
        verification_error = execution.sanitized_error
        if verification_error is None and not schema_verified:
            verification_error = (
                "Generated SQL did not reach the candidate target schema or pass every assertion."
            )
        return SimulationOutcome(dimensions, runs, legacy_results, verification_error)

    def _plan_failure_injection_runs(
        self,
        sandbox: PostgresSandbox,
        bundle: ProjectBundle,
        plan_sql: list[str],
        target: SnapshotSummary,
        baseline_columns: dict[str, list[str]],
    ) -> list[SimulationRun]:
        runs: list[SimulationRun] = []
        full_plan = ";\n".join(plan_sql)
        destructive = any(
            statement_kind(statement) in {"DROP", "TRUNCATE"}
            for statement in plan_sql
        )
        for boundary in range(len(plan_sql) + 1):
            started = time.monotonic()
            setup_error = self._restore_baseline(
                sandbox,
                bundle,
                include_seed=True,
            )
            if setup_error:
                runs.append(
                    SimulationRun(
                        id=str(uuid4()),
                        run_type="PLAN_FAILURE_INJECTION",
                        boundary=boundary,
                        status=EvidenceStatus.FAIL,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        sanitized_error=setup_error,
                    )
                )
                continue
            prefix_result: DatabaseExecution | None = None
            if boundary:
                prefix_result = sandbox.execute(
                    ";\n".join(plan_sql[:boundary])
                )
            partial = sandbox.snapshot(content_columns=baseline_columns)
            retry = sandbox.execute(full_plan)
            final = sandbox.snapshot(content_columns=baseline_columns)
            matches = (
                retry.succeeded
                and final.compatibility_schema_hash
                == target.compatibility_schema_hash
                and final.row_counts == target.row_counts
                and final.content_hashes == target.content_hashes
            )
            data_loss_possible = destructive and boundary > 0
            runs.append(
                SimulationRun(
                    id=str(uuid4()),
                    run_type="PLAN_FAILURE_INJECTION",
                    boundary=boundary,
                    status=(
                        EvidenceStatus.PASS if matches else EvidenceStatus.FAIL
                    ),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    snapshot=partial,
                    recovery=RecoveryAssessment(
                        classification=(
                            RecoveryClassification.EXTERNALLY_RECOVERABLE_ONLY
                            if data_loss_possible
                            else RecoveryClassification.FULLY_REVERSIBLE
                            if matches
                            else RecoveryClassification.FORWARD_FIX_REQUIRED
                        ),
                        retry_outcome=(
                            RetryOutcome.IDEMPOTENT
                            if boundary and matches
                            else RetryOutcome.SUCCESSFUL
                            if matches
                            else RetryOutcome.DUPLICATE_OR_CONFLICTING
                        ),
                        schema_matches_target=matches,
                        data_loss_possible=data_loss_possible,
                        summary=(
                            "Retry reached the verified generated-plan target."
                            if matches
                            else "Retry did not reach the verified generated-plan target."
                        ),
                    ),
                    sanitized_error=(
                        retry.sanitized_error
                        or (prefix_result.sanitized_error if prefix_result else None)
                    ),
                )
            )
        return runs

    def _restore_baseline(
        self, sandbox: PostgresSandbox, bundle: ProjectBundle, *, include_seed: bool
    ) -> str | None:
        sandbox.reset_database()
        for _, migration_sql in bundle.prior_migrations:
            result = sandbox.execute(migration_sql)
            if not result.succeeded:
                return result.sanitized_error or "Prior migration replay failed."
        if include_seed and bundle.seed_sql:
            result = sandbox.execute(bundle.seed_sql)
            if not result.succeeded:
                return result.sanitized_error or "Fixture loading failed."
        return None

    def _run_legacy_queries(
        self, sandbox: PostgresSandbox, bundle: ProjectBundle
    ) -> list[LegacyQueryResult]:
        results: list[LegacyQueryResult] = []
        for query in bundle.legacy_queries:
            execution = sandbox.execute(query.sql, wrap_rollback=True)
            expected_success = query.expected_outcome == "success"
            outcome_matches = execution.succeeded == expected_success
            affected_matches = (
                query.expected_affected_rows is None
                or execution.affected_rows == query.expected_affected_rows
            )
            results.append(
                LegacyQueryResult(
                    name=query.name,
                    query_hash=sql_hash(query.sql),
                    status=(
                        EvidenceStatus.PASS
                        if outcome_matches and affected_matches
                        else EvidenceStatus.FAIL
                    ),
                    duration_ms=execution.duration_ms,
                    affected_rows=execution.affected_rows,
                    sanitized_error=execution.sanitized_error,
                )
            )
        return results

    def _rollback_verification_run(
        self, sandbox: PostgresSandbox, bundle: ProjectBundle
    ) -> SimulationRun:
        started = time.monotonic()
        setup_error = self._restore_baseline(sandbox, bundle, include_seed=True)
        if setup_error:
            return SimulationRun(
                id=str(uuid4()),
                run_type="ROLLBACK_VERIFICATION",
                status=EvidenceStatus.FAIL,
                duration_ms=int((time.monotonic() - started) * 1000),
                sanitized_error=setup_error,
            )
        before = sandbox.snapshot()
        inverse, unsupported = _generate_inverse(sandbox, bundle)
        if unsupported is not None:
            classification = unsupported
            return SimulationRun(
                id=str(uuid4()),
                run_type="ROLLBACK_VERIFICATION",
                status=(
                    EvidenceStatus.NOT_TESTED
                    if classification is RecoveryClassification.NOT_APPLICABLE
                    else EvidenceStatus.FAIL
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
                snapshot=before,
                recovery=RecoveryAssessment(
                    classification=classification,
                    retry_outcome=RetryOutcome.NOT_ATTEMPTED,
                    schema_matches_target=False,
                    data_loss_possible=(
                        classification
                        is RecoveryClassification.EXTERNALLY_RECOVERABLE_ONLY
                    ),
                    summary=(
                        "No deterministic inverse exists for this destructive operation; external recovery evidence is required."
                        if classification
                        is RecoveryClassification.EXTERNALLY_RECOVERABLE_ONLY
                        else "The candidate contains no operation with a deterministic inverse."
                    ),
                ),
            )
        candidate = sandbox.execute(bundle.candidate_sql)
        if not candidate.succeeded:
            return SimulationRun(
                id=str(uuid4()),
                run_type="ROLLBACK_VERIFICATION",
                status=EvidenceStatus.FAIL,
                duration_ms=int((time.monotonic() - started) * 1000),
                sanitized_error=candidate.sanitized_error,
                recovery=RecoveryAssessment(
                    classification=RecoveryClassification.FORWARD_FIX_REQUIRED,
                    retry_outcome=RetryOutcome.NOT_ATTEMPTED,
                    schema_matches_target=False,
                    data_loss_possible=False,
                    summary="The candidate failed before its deterministic inverse could be tested.",
                ),
            )
        inverse_script = sandbox.execute_statements(inverse)
        after = sandbox.snapshot(content_columns=before.content_columns)
        schema_restored = (
            inverse_script.execution.succeeded and after.schema_hash == before.schema_hash
        )
        content_restored = schema_restored and all(
            after.content_hashes.get(table) == content_hash
            for table, content_hash in before.content_hashes.items()
        )
        if content_restored:
            classification = RecoveryClassification.FULLY_REVERSIBLE
            summary = "The inverse restored both the baseline schema and baseline-column content hashes."
        elif schema_restored:
            classification = RecoveryClassification.SCHEMA_REVERSIBLE
            summary = "The inverse restored the schema, but baseline-column content hashes prove data changed."
        else:
            classification = RecoveryClassification.FORWARD_FIX_REQUIRED
            summary = "The deterministic inverse did not restore the baseline schema; a forward fix is required."
        return SimulationRun(
            id=str(uuid4()),
            run_type="ROLLBACK_VERIFICATION",
            status=EvidenceStatus.PASS if schema_restored else EvidenceStatus.FAIL,
            duration_ms=int((time.monotonic() - started) * 1000),
            statements=_sql_statement_executions(inverse, inverse_script),
            snapshot=after,
            recovery=RecoveryAssessment(
                classification=classification,
                retry_outcome=RetryOutcome.NOT_ATTEMPTED,
                schema_matches_target=schema_restored,
                data_loss_possible=not content_restored,
                summary=summary,
            ),
            sanitized_error=inverse_script.execution.sanitized_error,
        )

    def _failure_injection_runs(
        self,
        sandbox: PostgresSandbox,
        bundle: ProjectBundle,
        findings: list[RiskFinding],
        target: SnapshotSummary,
        rollback_recovery: RecoveryAssessment | None = None,
    ) -> list[SimulationRun]:
        statement_count = len(bundle.candidate_statements)
        boundaries = list(range(statement_count + 1))
        destructive = any(f.category == "DESTRUCTIVE_DATA_LOSS" for f in findings)
        measured_data_loss = (
            rollback_recovery.data_loss_possible
            if rollback_recovery is not None
            else destructive
        )
        explicit_transaction = (
            statement_count >= 2
            and bundle.candidate_statements[0].kind == "BEGIN"
            and bundle.candidate_statements[-1].kind == "COMMIT"
        )
        runs: list[SimulationRun] = []
        for boundary in boundaries:
            started = time.monotonic()
            setup_error = self._restore_baseline(sandbox, bundle, include_seed=True)
            if setup_error:
                runs.append(
                    SimulationRun(
                        id=str(uuid4()),
                        run_type="FAILURE_INJECTION",
                        boundary=boundary,
                        status=EvidenceStatus.FAIL,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        sanitized_error=setup_error,
                    )
                )
                continue
            prefix_result: DatabaseExecution | None = None
            if boundary:
                prefix = ";\n".join(
                    statement.sql for statement in bundle.candidate_statements[:boundary]
                )
                prefix_result = sandbox.execute(prefix)
            partial = sandbox.snapshot(content_columns=target.content_columns)
            retry = sandbox.execute(bundle.candidate_sql)
            final = sandbox.snapshot(content_columns=target.content_columns)
            matches = retry.succeeded and final.schema_hash == target.schema_hash
            data_loss_possible = (
                measured_data_loss and boundary > 0 and not explicit_transaction
            )
            if matches:
                retry_outcome = (
                    RetryOutcome.IDEMPOTENT if boundary else RetryOutcome.SUCCESSFUL
                )
                classification = (
                    RecoveryClassification.EXTERNALLY_RECOVERABLE_ONLY
                    if data_loss_possible
                    else RecoveryClassification.FULLY_REVERSIBLE
                )
            else:
                retry_outcome = RetryOutcome.DUPLICATE_OR_CONFLICTING
                classification = (
                    RecoveryClassification.EXTERNALLY_RECOVERABLE_ONLY
                    if data_loss_possible
                    else RecoveryClassification.FORWARD_FIX_REQUIRED
                )
            summary = (
                "The open transaction rolled back when the client disconnected; retry reached the target schema."
                if explicit_transaction and matches
                else "Retry reached the expected target schema."
                if matches
                else "Retry did not reach the expected target schema and requires a forward fix."
            )
            runs.append(
                SimulationRun(
                    id=str(uuid4()),
                    run_type="FAILURE_INJECTION",
                    boundary=boundary,
                    status=EvidenceStatus.PASS if matches else EvidenceStatus.FAIL,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    snapshot=partial,
                    recovery=RecoveryAssessment(
                        classification=classification,
                        retry_outcome=retry_outcome,
                        schema_matches_target=matches,
                        data_loss_possible=data_loss_possible,
                        summary=summary,
                    ),
                    sanitized_error=(
                        retry.sanitized_error
                        or (prefix_result.sanitized_error if prefix_result else None)
                    ),
                )
            )
        return runs

    def _failed_candidate_retry(
        self,
        sandbox: PostgresSandbox,
        bundle: ProjectBundle,
        findings: list[RiskFinding],
    ) -> SimulationRun:
        started = time.monotonic()
        setup_error = self._restore_baseline(sandbox, bundle, include_seed=True)
        retry = (
            sandbox.execute(bundle.candidate_sql)
            if not setup_error
            else DatabaseExecution(False, 0, "", setup_error, None)
        )
        destructive = any(f.category == "DESTRUCTIVE_DATA_LOSS" for f in findings)
        return SimulationRun(
            id=str(uuid4()),
            run_type="FAILURE_INJECTION",
            boundary=0,
            status=EvidenceStatus.PASS if retry.succeeded else EvidenceStatus.FAIL,
            duration_ms=int((time.monotonic() - started) * 1000),
            snapshot=sandbox.snapshot() if not setup_error else None,
            recovery=RecoveryAssessment(
                classification=(
                    RecoveryClassification.EXTERNALLY_RECOVERABLE_ONLY
                    if destructive
                    else RecoveryClassification.FORWARD_FIX_REQUIRED
                ),
                retry_outcome=(
                    RetryOutcome.SUCCESSFUL
                    if retry.succeeded
                    else RetryOutcome.DUPLICATE_OR_CONFLICTING
                ),
                schema_matches_target=False,
                data_loss_possible=destructive,
                summary="A clean retry reproduced the candidate result; no safe target schema was established.",
            ),
            sanitized_error=retry.sanitized_error,
        )


def empty_dimensions() -> list[EvidenceDimension]:
    definitions = (
        ("prior_migrations", "Prior migrations"),
        ("fixture_load", "Synthetic fixtures"),
        ("schema_application", "Schema applied"),
        ("data_preservation", "Data preserved"),
        ("legacy_queries", "Legacy queries"),
        ("rollback_recovery", "Rollback recovery"),
        ("failure_recovery", "Failure recovery"),
        ("idempotent_retry", "Idempotent retry"),
    )
    return [
        EvidenceDimension(
            key=key,
            label=label,
            status=EvidenceStatus.NOT_TESTED,
            source=EvidenceSource.STATIC,
            summary="This dimension has not been tested.",
        )
        for key, label in definitions
    ]


_IDENTIFIER = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
_TABLE_IDENTIFIER = rf'(?:{_IDENTIFIER}\.)?{_IDENTIFIER}'


def _generate_inverse(
    sandbox: PostgresSandbox, bundle: ProjectBundle
) -> tuple[list[str], RecoveryClassification | None]:
    inverse: list[str] = []
    for policy_statement in reversed(bundle.candidate_statements):
        statement = policy_statement.sql.strip()
        add_column = re.match(
            rf"^ALTER\s+TABLE\s+({_TABLE_IDENTIFIER})\s+ADD\s+(?:COLUMN\s+)?"
            rf"(?:IF\s+NOT\s+EXISTS\s+)?({_IDENTIFIER})\b",
            statement,
            re.IGNORECASE,
        )
        if add_column:
            inverse.append(
                f"ALTER TABLE {add_column.group(1)} DROP COLUMN {add_column.group(2)}"
            )
            continue
        create_index = re.match(
            rf"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?({_TABLE_IDENTIFIER})\b",
            statement,
            re.IGNORECASE,
        )
        if create_index:
            inverse.append(f"DROP INDEX {create_index.group(1)}")
            continue
        add_constraint = re.match(
            rf"^ALTER\s+TABLE\s+({_TABLE_IDENTIFIER})\s+ADD\s+CONSTRAINT\s+({_IDENTIFIER})\b",
            statement,
            re.IGNORECASE,
        )
        if add_constraint:
            inverse.append(
                f"ALTER TABLE {add_constraint.group(1)} DROP CONSTRAINT {add_constraint.group(2)}"
            )
            continue
        create_table = re.match(
            rf"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_TABLE_IDENTIFIER})\b",
            statement,
            re.IGNORECASE,
        )
        if create_table:
            inverse.append(f"DROP TABLE {create_table.group(1)}")
            continue
        rename_column = re.match(
            rf"^ALTER\s+TABLE\s+({_TABLE_IDENTIFIER})\s+RENAME\s+COLUMN\s+"
            rf"({_IDENTIFIER})\s+TO\s+({_IDENTIFIER})\s*$",
            statement,
            re.IGNORECASE,
        )
        if rename_column:
            inverse.append(
                f"ALTER TABLE {rename_column.group(1)} RENAME COLUMN "
                f"{rename_column.group(3)} TO {rename_column.group(2)}"
            )
            continue
        rename_table = re.match(
            rf"^ALTER\s+TABLE\s+({_TABLE_IDENTIFIER})\s+RENAME\s+TO\s+({_IDENTIFIER})\s*$",
            statement,
            re.IGNORECASE,
        )
        if rename_table:
            old_name = rename_table.group(1).split(".")[-1]
            inverse.append(
                f"ALTER TABLE {rename_table.group(2)} RENAME TO {old_name}"
            )
            continue
        set_not_null = re.match(
            rf"^ALTER\s+TABLE\s+({_TABLE_IDENTIFIER})\s+ALTER\s+(?:COLUMN\s+)?"
            rf"({_IDENTIFIER})\s+SET\s+NOT\s+NULL\s*$",
            statement,
            re.IGNORECASE,
        )
        if set_not_null:
            inverse.append(
                f"ALTER TABLE {set_not_null.group(1)} ALTER COLUMN "
                f"{set_not_null.group(2)} DROP NOT NULL"
            )
            continue
        set_default = re.match(
            rf"^ALTER\s+TABLE\s+({_TABLE_IDENTIFIER})\s+ALTER\s+(?:COLUMN\s+)?"
            rf"({_IDENTIFIER})\s+SET\s+DEFAULT\b",
            statement,
            re.IGNORECASE,
        )
        if set_default:
            inverse.append(
                f"ALTER TABLE {set_default.group(1)} ALTER COLUMN "
                f"{set_default.group(2)} DROP DEFAULT"
            )
            continue
        drop_column = re.match(
            rf"^ALTER\s+TABLE\s+({_TABLE_IDENTIFIER})\s+DROP\s+(?:COLUMN\s+)?"
            rf"(?:IF\s+EXISTS\s+)?({_IDENTIFIER})\b",
            statement,
            re.IGNORECASE,
        )
        if drop_column:
            restore = _column_restore_sql(
                sandbox, drop_column.group(1), drop_column.group(2)
            )
            if restore is None:
                return [], RecoveryClassification.EXTERNALLY_RECOVERABLE_ONLY
            inverse.extend(restore)
            continue
        if re.match(r"^(?:TRUNCATE|DROP\s+(?:TABLE|INDEX))\b", statement, re.IGNORECASE):
            return [], RecoveryClassification.EXTERNALLY_RECOVERABLE_ONLY
        return [], RecoveryClassification.NOT_APPLICABLE
    if not inverse:
        return [], RecoveryClassification.NOT_APPLICABLE
    return inverse, None


def _column_restore_sql(
    sandbox: PostgresSandbox, table: str, column: str
) -> list[str] | None:
    table_literal = table.replace("'", "''")
    column_name = column.strip('"')
    column_literal = column_name.replace("'", "''")
    metadata = sandbox.execute(
        "SELECT format_type(a.atttypid, a.atttypmod) || '|' || "
        "a.attnotnull::text || '|' || COALESCE(pg_get_expr(d.adbin, d.adrelid), '') "
        "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid "
        "AND d.adnum = a.attnum "
        f"WHERE a.attrelid = '{table_literal}'::regclass "
        f"AND a.attname = '{column_literal}' AND NOT a.attisdropped"
    )
    if not metadata.succeeded or not metadata.output.strip():
        return None
    data_type, not_null, default = metadata.output.strip().split("|", 2)
    add = f"ALTER TABLE {table} ADD COLUMN {column} {data_type}"
    if default:
        add += f" DEFAULT {default}"
    statements = [add]
    if not_null.lower() == "true" or not_null.lower() == "t":
        if not default:
            statements.append(
                f"UPDATE {table} SET {column} = {_rollback_placeholder(data_type)} "
                f"WHERE {column} IS NULL"
            )
        statements.append(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")
    return statements


def _rollback_placeholder(data_type: str) -> str:
    normalized = data_type.lower()
    if any(token in normalized for token in ("int", "numeric", "decimal", "real", "double")):
        return "0"
    if "bool" in normalized:
        return "FALSE"
    if "uuid" in normalized:
        return "'00000000-0000-4000-8000-000000000000'"
    if "timestamp" in normalized or normalized == "date":
        return "CURRENT_TIMESTAMP"
    if "json" in normalized:
        return "'{}'"
    return "'rollbackready-restored'"


def _set_dimension(
    dimensions: list[EvidenceDimension],
    key: str,
    status: EvidenceStatus,
    source: EvidenceSource,
    summary: str,
) -> None:
    for index, dimension in enumerate(dimensions):
        if dimension.key == key:
            dimensions[index] = EvidenceDimension(
                key=key,
                label=dimension.label,
                status=status,
                source=source,
                summary=summary,
            )
            return


def _statement_executions(
    bundle: ProjectBundle, result: ScriptExecution
) -> list[StatementExecution]:
    explicit_transaction = (
        len(bundle.candidate_statements) >= 2
        and bundle.candidate_statements[0].kind == "BEGIN"
        and bundle.candidate_statements[-1].kind == "COMMIT"
    )
    completed = set(result.completed_statement_indexes)
    failed = result.failed_statement_index
    return [
        StatementExecution(
            statement_index=statement.index,
            statement_shape=statement.shape,
            status=(
                EvidenceStatus.PASS
                if statement.index in completed
                else EvidenceStatus.FAIL
                if statement.index == failed
                else EvidenceStatus.NOT_TESTED
            ),
            duration_ms=(
                result.execution.duration_ms
                if statement.index == (failed or len(bundle.candidate_statements))
                else 0
            ),
            affected_rows=_affected_rows_for_statement(result, statement.index),
            sanitized_error=(
                result.execution.sanitized_error
                if statement.index == failed
                else None
            ),
            rolled_back=(
                explicit_transaction
                and not result.execution.succeeded
                and statement.index in completed
            ),
        )
        for statement in bundle.candidate_statements
    ]


def _affected_rows_for_statement(
    result: ScriptExecution, statement_index: int
) -> int | None:
    output = result.statement_outputs.get(statement_index, "")
    matches = re.findall(
        r"^(?:INSERT\s+\d+|UPDATE|DELETE|MERGE|COPY)\s+(\d+)\s*$",
        output,
        re.MULTILINE,
    )
    return int(matches[-1]) if matches else None


def _sql_statement_executions(
    statements: list[str], result: ScriptExecution
) -> list[StatementExecution]:
    completed = set(result.completed_statement_indexes)
    failed = result.failed_statement_index
    return [
        StatementExecution(
            statement_index=index,
            statement_shape=redact_sql(statement),
            status=(
                EvidenceStatus.PASS
                if index in completed
                else EvidenceStatus.FAIL
                if index == failed
                else EvidenceStatus.NOT_TESTED
            ),
            duration_ms=(
                result.execution.duration_ms
                if index == (failed or len(statements))
                else 0
            ),
            affected_rows=_affected_rows_for_statement(result, index),
            sanitized_error=(
                result.execution.sanitized_error if index == failed else None
            ),
        )
        for index, statement in enumerate(statements, start=1)
    ]


def _verification_assertion_passed(execution: DatabaseExecution) -> bool:
    if not execution.succeeded:
        return False
    values = [
        value.strip().lower()
        for value in execution.output.splitlines()
        if value.strip().lower() not in {"", "begin", "commit", "rollback"}
    ]
    return bool(values) and all(value in {"0", "t", "true"} for value in values)
