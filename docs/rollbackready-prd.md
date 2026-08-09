# RollbackReady

## Product Requirements Document

**Migration Safety Agent for Prisma**

| Document field | Value |
|---|---|
| Version | 1.0 |
| Status | MVP implemented; release candidate pending merge and deployment approval |
| Date | 2026-08-09 |
| Audience | Hackathon judges, product reviewers, and implementation team |
| Owner | NYC Round 3 Session 1 Hackathon Team |
| MVP horizon | Evidence-first six-hour solo build |
| Product horizon | MVP plus production roadmap |

> **Product promise:** Prisma checks whether a migration can be applied. RollbackReady checks whether representative data, the previous application contract, and the recovery path can survive it.

---

## 1. Executive summary

RollbackReady is a pre-deployment safety layer for PostgreSQL projects that use Prisma Migrate. It accepts a Prisma migration history, a candidate migration, synthetic production-shaped data, and legacy SQL queries. It then performs deterministic static checks, reconstructs the pre-migration database in a disposable PostgreSQL sandbox, applies the migration, replays the legacy contract, injects failures, tests retries and recovery, and produces an evidence-backed report.

When the original migration is unsafe, RollbackReady may ask Gemini for a phased expand-and-contract plan. The model is allowed to explain findings and propose candidate SQL. It is not allowed to decide that its own plan is correct. Every generated phase is reparsed, policy-checked, and rerun from a clean PostgreSQL baseline. Only deterministic verification can produce the product's strongest verdict: `VERIFIED_FOR_REVIEW`.

RollbackReady never claims that a migration is universally safe or automatically deploys SQL. It never connects to production. Its output is evidence for a human reviewer, bounded by the supplied history, fixtures, queries, engine version, and test limits.

### 1.1 Feasibility verdict

The project is technically feasible and has a credible product thesis. The full authenticated SaaS platform is not feasible for one developer in six hours. A focused, judgeable MVP is feasible if it preserves the technical differentiator and defers platform breadth.

The six-hour MVP includes:

- PostgreSQL-only Prisma projects.
- Complete migration-history replay.
- Four deterministic risk-rule families.
- Real disposable PostgreSQL execution.
- Synthetic fixture loading and legacy-query replay.
- Statement-boundary failure injection and retry classification.
- Gemini-generated plans with strict structured output.
- Fresh-sandbox verification of generated SQL.
- A compact six-stage web stepper and evidence report.

The six-hour MVP excludes Clerk accounts, long-term user history, GitHub integration, multiple database engines, production cloning, distributed workers, measured production lock duration, and automated deployment.

### 1.2 Current implementation readiness

The MVP is implemented on the synchronized `codex/rollbackready-mvp` branches in the backend and frontend repositories. Both draft pull requests are mergeable and their current GitHub Actions checks pass. The implementation includes the complete upload and built-in-demo flow, deterministic rules, PostgreSQL 18 simulation, content-preservation hashes, statement-boundary failure and retry evidence, LangGraph-orchestrated Gemini planning with deterministic fallback, fresh-baseline plan verification, owner-aware sanitized report persistence, expiry cleanup, rate limits, and the six-stage responsive frontend.

The earlier baseline blockers are resolved. Ruff, compilation, backend tests, frontend linting, TypeScript checks, and production builds pass. The migration history includes sanitized evidence and Clerk ownership migrations. Vertex AI is enabled and the backend runtime identity has `roles/aiplatform.user`. The simulator image packages PostgreSQL 18, Cloud Run is configured for a single 2 GiB instance, and the GKE manifests provide a single control pod with bounded memory and ephemeral storage.

The release candidate has not been merged or promoted because deployment requires separate approval. The live Cloud Run services therefore still run the earlier foundation revisions. Clerk authentication and ownership isolation are implemented as an optional path, but no Clerk publishable or secret keys are configured in GitHub or Google Secret Manager; the supported hackathon configuration remains anonymous opaque IDs with 24-hour sanitized-report expiry.

---

## 2. Problem and opportunity

### 2.1 User problem

Prisma Migrate produces customizable SQL migrations and provides strong development workflows. In production, `prisma migrate deploy` primarily checks migration history and applies pending migration files. It does not detect schema drift and does not use a shadow database [1]. Prisma also documents that the full migration directory is the source of truth; `schema.prisma` alone cannot represent customized migration history [2].

A migration that applies successfully can still be operationally unsafe:

- Existing data can violate a new `NOT NULL`, `UNIQUE`, or `CHECK` constraint.
- Old application instances can fail during a rolling deployment after a rename or drop.
- An interrupted multi-statement migration can leave a partially modified database.
- Retrying an incomplete migration can fail or duplicate work.
- A schema-level reversal can recreate structure without restoring deleted data.
- A lock-prone statement can create downtime even if it is structurally correct.
- Generated remediation SQL can be syntactically valid while semantically wrong.

Developers need a pre-deployment answer to a different question: **what evidence shows that this migration, data shape, compatibility contract, interruption point, and recovery strategy survive together?**

### 2.2 Market distinction

Static migration safety is an established category. Atlas analyzes destructive, data-dependent, backward-incompatible, and lock-related changes [3]. Squawk provides PostgreSQL migration linting and CI integration [4]. Prisma now documents pgfence as a pre-deploy safety check [5]. RollbackReady must not position ordinary SQL linting as its invention.

| Capability | Prisma Migrate | Static linters | RollbackReady |
|---|---|---|---|
| Apply pending migrations | Yes | No | Sandbox only |
| Static destructive-change rules | Some development warnings | Yes | Yes |
| Reconstruct full Prisma history | Development workflow | Tool-dependent | Required for verified mode |
| Test production-shaped fixtures | No | Usually no | Yes |
| Replay previous app queries | No | Static inference at most | Yes |
| Inject partial failures | No | No | Yes |
| Test retry and recovery | No | No | Yes |
| Classify data reversibility | Limited guidance | Tool-dependent | Explicit evidence dimension |
| Generate phased remediation | Manual | Recipes/tool-dependent | Gemini candidate plan |
| Verify generated remediation | No | No | Fresh deterministic simulation |
| Connect to production | Production deploy command can | Not required | Never |

RollbackReady's defensible wedge is the combination of Prisma-first history replay, representative data, executable legacy contracts, failure experiments, recovery classification, and deterministic validation of AI-generated plans.

---

## 3. Product vision, users, and jobs

### 3.1 Vision

Make high-risk schema changes reviewable as evidence, not intuition. Every claim should link to a rule, SQL statement, fixture result, query result, snapshot difference, or reproducible simulation event.

### 3.2 Primary persona

**Prisma application team:** a full-stack developer or technical lead at a small-to-medium team shipping a Node.js/TypeScript application backed by PostgreSQL and using Prisma Migrate in CI/CD.

Primary characteristics:

- Owns application code and migrations but may not have a dedicated DBA.
- Uses rolling or zero-downtime deployment patterns.
- Wants actionable explanations rather than database jargon alone.
- Needs a fast pull-request or pre-release confidence check.
- Cannot safely provide production credentials to a hackathon tool.

### 3.3 Secondary personas

- **Reviewer or tech lead:** needs traceable evidence and a clear reason to block or approve human review.
- **Platform engineer:** wants policy thresholds, repeatable reports, and eventual CI integration.
- **Database specialist:** wants raw SQL, query failures, snapshot differences, and honest limitations.

### 3.4 Jobs to be done

1. When I am about to merge a Prisma migration, show me what can destroy data, reject existing rows, or break the old application.
2. When a migration contains several statements, show me what remains after interruption and whether retry is safe.
3. When reversal cannot restore data, distinguish schema recovery from data recovery.
4. When a migration is unsafe, propose a phased alternative and prove whether it works on the supplied evidence.
5. When evidence is missing, tell me exactly what was not tested instead of manufacturing confidence.

---

## 4. Goals, non-goals, and success metrics

### 4.1 Goals

- Detect and explain the four MVP risk families with deterministic rules.
- Execute supported PostgreSQL migrations against a disposable PostgreSQL 18 instance.
- Reproduce the pre-migration state by replaying complete prior Prisma migrations.
- Demonstrate data-dependent failures using synthetic fixtures.
- Demonstrate rolling-deployment compatibility using legacy queries.
- Demonstrate interruption, retry, and recovery outcomes at statement boundaries.
- Produce a phased candidate plan through Gemini when deterministic findings justify it.
- Verify every candidate plan from a clean baseline before displaying a positive verdict.
- Produce a report suitable for technical review and a five-minute hackathon tour.

### 4.2 Non-goals for the MVP

- Connecting to, cloning, modifying, or profiling a production database.
- Supporting MySQL, MariaDB, SQLite, SQL Server, CockroachDB, or MongoDB.
- Proving production lock duration or throughput from small synthetic data.
- Executing arbitrary PostgreSQL superuser or server-administration statements.
- Automatically editing the user's repository or deploying generated SQL.
- Providing a universal guarantee of safety.
- Implementing Clerk accounts, teams, billing, GitHub checks, or long-term histories.
- Reconstructing application behavior from source code; the legacy contract is explicit SQL.

### 4.3 Hackathon acceptance metrics

| Metric | Target |
|---|---|
| Broad feature demonstration | Under 5 minutes |
| Individual deterministic simulation stage | Under 90 seconds at MVP limits |
| Built-in unsafe example | Original migration blocked; phased plan verified for review |
| Deterministic rule coverage | All four rule families demonstrated |
| Legacy-query evidence | At least two old-version queries replayed |
| Failure evidence | Every supported statement boundary evaluated |
| Unverified safety claims | Zero |
| Production connections or execution | Zero |
| Raw fixture values sent to Gemini | Zero |

---

## 5. Safety language and evidence model

### 5.1 Overall verdicts

| Verdict | Meaning |
|---|---|
| `UNSAFE` | One or more critical deterministic checks failed, or the migration caused unrecoverable loss under supplied evidence. |
| `CONDITIONALLY_VERIFIED` | Supported checks passed, but one or more required evidence dimensions were not tested or only heuristic. |
| `VERIFIED_FOR_REVIEW` | All required evidence dimensions passed on a fresh supported sandbox. Human review is still required. |
| `INSUFFICIENT_EVIDENCE` | The upload cannot reconstruct or meaningfully exercise the pre-migration state. |
| `ERROR` | The analysis failed because of invalid input, unsupported SQL, sandbox failure, timeout, or internal error. |

`STATIC_ANALYSIS_ONLY` is an evidence level, not an overall verdict. Static-only analyses cannot receive `VERIFIED_FOR_REVIEW`.

### 5.2 Evidence dimensions

Every dimension is independently reported as `PASS`, `FAIL`, or `NOT_TESTED`:

- Migration application.
- Target-schema integrity.
- Fixture preservation.
- Legacy-query compatibility.
- Interruption recovery.
- Idempotent retry.
- Generated-plan verification.
- Lock-risk heuristic.

### 5.3 Recovery classifications

| Classification | Meaning |
|---|---|
| `FULLY_REVERSIBLE` | Schema and original fixture data can be restored from the simulated state. |
| `SCHEMA_REVERSIBLE` | Structure can be restored but original data cannot be reconstructed. |
| `FORWARD_FIX_REQUIRED` | Reversal is unsafe; recovery requires a new forward migration. |
| `EXTERNAL_RECOVERY_REQUIRED` | Recovery depends on a backup or external source not supplied to RollbackReady. |

The report must never collapse `SCHEMA_REVERSIBLE` into “rollback succeeded.”

---

## 6. User experience

### 6.1 Primary journey

1. The user opens RollbackReady and selects the built-in demo or uploads a project archive.
2. RollbackReady validates the archive and asks the user to select the candidate migration folder.
3. The user starts analysis and sees the evidence level and supported/unsupported checks.
4. The results view shows the schema comparison, risk cards, failed statements, fixture effects, and legacy-query results.
5. The failure lab shows each interruption point, remaining state, retry result, and recovery class.
6. If the migration is unsafe, the user requests a safer plan.
7. Gemini returns a phased plan that is visibly labeled unverified.
8. The user triggers verification; RollbackReady reruns the plan from a clean baseline.
9. The final report distinguishes the original migration from the generated plan and exposes all evidence and limitations.

### 6.2 Compact six-stage stepper

The MVP uses one cohesive flow instead of six disconnected applications:

1. **Upload** - archive validation, candidate selection, fixture/query coverage.
2. **Compare** - before/after schema objects and candidate statements.
3. **Risks** - severity cards and deterministic findings.
4. **Failure lab** - statement-boundary timeline and recovery matrix.
5. **Safer plan** - expand, deploy, backfill, verify, and contract phases.
6. **Evidence report** - verdict, tested dimensions, limitations, and export-ready summary.

### 6.3 Built-in demo

The judge-facing fixture contains:

- A `User` table with three rows.
- A candidate migration adding `phone TEXT NOT NULL` without a default or backfill.
- An old registration query that omits `phone`.
- An old profile query selecting the original columns.
- A generated expand-and-contract plan that adds a nullable column, backfills deterministic fixture values, verifies zero nulls, and applies `NOT NULL` only after compatibility is demonstrated.

The final demo screen must say:

```text
Original migration: UNSAFE
Generated plan: VERIFIED FOR REVIEW

Data preserved: PASS
Legacy queries: PASS
Failure recovery: PASS
Idempotent retry: PASS
Production execution: NOT PERFORMED
```

---

## 7. Input and output contracts

### 7.1 Project archive

The user uploads one `.zip` archive. The MVP accepts at most 10 MiB compressed and 50 MiB uncompressed. Archive entries must be relative, normalized paths; symlinks, path traversal, nested archives, and duplicate normalized paths are rejected.

```text
project.zip
|-- prisma/
|   |-- schema.prisma
|   `-- migrations/
|       |-- migration_lock.toml
|       |-- 20260808090000_init/
|       |   `-- migration.sql
|       `-- 20260809100000_add_phone/
|           `-- migration.sql
`-- rollbackready/
    |-- seed.sql
    `-- legacy-queries.json
```

The upload form sends the selected candidate migration folder as a separate `candidate_migration` field. Every lexicographically earlier migration is treated as prior history. A candidate whose ordering is ambiguous or whose provider does not match `migration_lock.toml` is rejected.

### 7.2 Verified-mode requirements

`VERIFIED_FOR_REVIEW` requires:

- PostgreSQL provider in `schema.prisma` and `migration_lock.toml`.
- Complete prior migration history that replays successfully.
- A distinct candidate `migration.sql`.
- Synthetic `seed.sql` that loads successfully.
- At least one legacy query.
- Candidate and generated-plan SQL entirely within the supported statement policy.
- Successful fresh-sandbox verification.

If history, fixtures, or legacy queries are absent, RollbackReady may still produce findings, but missing dimensions become `NOT_TESTED` and the evidence level is reduced.

### 7.3 Legacy query contract

```json
[
  {
    "name": "old-user-profile-query",
    "sql": "SELECT id, name, email FROM users WHERE id = 1",
    "expected_outcome": "success"
  },
  {
    "name": "old-user-registration",
    "sql": "INSERT INTO users (name, email) VALUES ('Aman', 'a@example.com')",
    "expected_outcome": "success",
    "expected_affected_rows": 1
  }
]
```

MVP limits are 20 legacy queries and one SQL statement per entry. Queries run only inside restored sandbox snapshots. The contract supports `SELECT`, `INSERT`, `UPDATE`, and `DELETE`; server-level commands and transaction-control statements are rejected.

### 7.4 Persistent output

The persistent report may include:

- Artifact hashes and byte counts.
- Migration folder names and normalized statement shapes.
- Sanitized finding excerpts.
- Schema object names.
- Evidence statuses and aggregate counts.
- Timeline events and recovery classifications.
- Generated plan SQL and its verification results.
- Model identifier and prompt-template version.

The persistent report must not include raw uploaded files, fixture rows, production-looking literals, database credentials, or unsanitized model prompts.

---

## 8. Functional requirements

| ID | Requirement | MVP acceptance |
|---|---|---|
| FR-01 | Validate and stage a Prisma project archive. | Valid demo archive accepted; traversal, oversize, and provider mismatch rejected. |
| FR-02 | Reconstruct the pre-candidate schema from prior migration history. | Prior migrations replay in order on a fresh PostgreSQL 18 sandbox. |
| FR-03 | Load synthetic fixtures. | `seed.sql` succeeds within limits and fixture counts are recorded without persisting values. |
| FR-04 | Parse and classify candidate SQL. | Supported statements are split deterministically and assigned risk findings. |
| FR-05 | Execute the candidate normally. | Application status, errors, schema diff, and fixture counts are recorded. |
| FR-06 | Replay the legacy query contract. | Every query produces pass/fail, sanitized error, duration, and affected-row metadata. |
| FR-07 | Inject failures at statement boundaries. | Baseline is restored for each boundary and remaining state is captured. |
| FR-08 | Test retry behavior. | Retry outcome is reported as successful, idempotent, duplicate/conflicting, or unsupported. |
| FR-09 | Classify recovery. | Every destructive or partial scenario receives one recovery classification. |
| FR-10 | Generate a safer plan. | Gemini output validates against the RecoveryPlan schema or deterministic fallback is used. |
| FR-11 | Verify the plan. | Plan is blocked until a clean-sandbox run finishes; positive verdict requires all mandatory dimensions. |
| FR-12 | Produce an evidence report. | Report distinguishes rules, execution evidence, heuristics, untested dimensions, and limitations. |
| FR-13 | Delete raw artifacts. | Temporary files and PostgreSQL data directory are removed after the lifecycle or timeout. |
| FR-14 | Provide the built-in demo. | Demo can be loaded without user files and completes the full flow. |

---

## 9. Deterministic risk engine

### 9.1 Rule family A: destructive and data-loss operations

Flag at minimum:

- `DROP TABLE`, `DROP COLUMN`, and `TRUNCATE`.
- Type narrowing or lossy casts.
- Enum-value removal.
- Replacement patterns that drop an old column before data copy.
- Immediate rename/drop patterns that remove the old contract.

The finding records severity, category, statement index, normalized SQL shape, affected object, reason, evidence source, and remediation hint.

### 9.2 Rule family B: constraints against existing data

Flag and test:

- Non-null columns added without a default/backfill.
- Nullable columns changed to non-null.
- Unique indexes or constraints when duplicates exist.
- Check constraints violated by fixture rows.
- Foreign keys whose referenced rows are absent.

Static findings are warnings until fixture execution confirms or disproves them. The report must identify which conclusion came from SQL shape and which came from data execution.

### 9.3 Rule family C: backward compatibility

Detect renamed, dropped, newly required, or type-incompatible objects referenced by legacy queries. Deterministic execution after migration is authoritative for the supplied contract. A legacy query failure prevents `VERIFIED_FOR_REVIEW`.

### 9.4 Rule family D: partial execution and idempotency

For a migration with `N` supported statements, run clean experiments for failure before statement 1 and after each statement through `N - 1`. For each experiment:

1. Restore the pre-migration snapshot.
2. Execute statements through the selected boundary.
3. Close the client connection to model interruption.
4. Capture schema and fixture-count state.
5. Retry the candidate according to the MVP retry strategy.
6. Capture the retry error or end state.
7. Compare against the expected target schema.
8. Assign recovery and idempotency classifications.

Explicit `BEGIN`/`COMMIT` blocks are preserved as transaction groups. The report distinguishes transaction rollback from non-transactional partial state.

### 9.5 Lock-risk heuristic

Flag patterns such as non-concurrent index creation, table rewrites, type changes, validation-heavy constraints, and long backfills. The report must label this result `HEURISTIC` and state that synthetic execution does not measure production lock time.

---

## 10. Simulation architecture

The control flow is: Next.js evidence stepper -> FastAPI analysis API -> archive and policy validator -> deterministic risk engine -> sandbox manager -> disposable PostgreSQL 18. The compatibility and recovery engine writes only sanitized evidence. When remediation is requested, normalized findings pass to Gemini, then through Pydantic and SQL policy validation, and finally into a fresh sandbox before the report can show a verified verdict.

### 10.1 MVP sandbox

Each run starts an unprivileged PostgreSQL 18 cluster under a unique `/tmp/rollbackready/{analysis_id}` directory. It listens on a Unix socket only. The control process creates a non-superuser database role for migration execution. The sandbox has no route to the production database and receives no production credentials.

The worker enforces:

- One active simulation per application instance.
- 10 MiB compressed and 50 MiB uncompressed upload limits.
- 25 candidate statements.
- 20 legacy queries.
- 10,000 fixture rows across supported demo tables.
- 5-second statement timeout and 2-second lock timeout by default.
- 90-second maximum per simulation stage.
- 1 GiB temporary storage budget and bounded process memory.
- Cleanup on success, failure, timeout, and startup recovery.

### 10.2 SQL policy

Allow only supported database-local DDL and fixture/query DML. Block at minimum:

- `CREATE DATABASE`, `DROP DATABASE`, `CREATE ROLE`, `ALTER ROLE`, and privilege escalation.
- `COPY ... PROGRAM` and server-side filesystem access.
- Extension installation, procedural untrusted languages, event triggers, replication commands, and configuration changes outside the session allowlist.
- Network extensions and functions known to perform external access.
- Unbounded sleep or deliberate resource-exhaustion constructs.

Unsupported SQL produces `ERROR` or `INSUFFICIENT_EVIDENCE`; it is never silently skipped in verified mode.

### 10.3 Analysis state machine

- `STAGED` moves to `VALIDATING`.
- Validation failure ends as `INVALID`; valid input moves to `ANALYZING`.
- Incomplete evidence ends as `STATIC_ONLY`; verified-mode input moves to `SIMULATING`.
- Simulation ends as `UNSAFE`, `CONDITIONAL`, or `VERIFIED` according to deterministic evidence.
- An `UNSAFE` analysis may enter `PLANNING` when the user requests remediation.
- Schema or policy failure ends as `PLAN_REJECTED`; otherwise the plan enters `VERIFYING_PLAN`.
- Fresh-sandbox failure ends as `PLAN_REJECTED`; complete mandatory evidence ends as `VERIFIED_PLAN`.

---

## 11. Gemini planner boundaries

### 11.1 Provider and model

Use the Google Gen AI SDK against Vertex AI with workload identity. Default to the stable `gemini-3.6-flash` model [6]. The model identifier remains configurable through `GEMINI_MODEL`, but production configuration must use an explicit stable identifier, not a floating `latest` alias.

Generation configuration:

- Temperature: `0.1`.
- Structured JSON response schema generated from Pydantic.
- No web search, code execution, URL retrieval, or autonomous tool use.
- One generation attempt plus one schema-repair attempt.
- Deterministic fallback template if generation is unavailable or invalid.

### 11.2 Allowed AI responsibilities

- Explain deterministic findings in plain language.
- Select an allowed strategy such as expand-and-contract or forward fix.
- Propose candidate SQL phases.
- Propose preconditions, verification queries, and recovery actions.
- Explain why a plan failed deterministic verification.

### 11.3 Prohibited AI responsibilities

- Connect to any database.
- Execute SQL directly.
- Read raw fixture values.
- Override deterministic findings.
- Suppress unsupported or untested evidence.
- Mark a migration safe or verified.
- Deploy, commit, or modify the user's project.

### 11.4 Recovery plan contract

```json
{
  "risk_summary": "Required column added to a populated table",
  "strategy": "expand_contract",
  "assumptions": ["The application can tolerate NULL phone values during transition"],
  "phases": [
    {
      "name": "expand",
      "purpose": "Add a backward-compatible nullable column",
      "sql": ["ALTER TABLE users ADD COLUMN phone TEXT"],
      "preconditions": ["users table exists"],
      "verification_sql": ["SELECT COUNT(*) FROM users WHERE phone IS NULL"],
      "expected": "Known baseline count",
      "recovery_action": "Drop the new column only if no trusted values have been written"
    }
  ]
}
```

The plan is displayed as `UNVERIFIED_CANDIDATE` until policy validation and fresh-sandbox execution finish.

---

## 12. Public API

All MVP endpoints use `/api/v1`. Analysis IDs and plan IDs are opaque UUIDv4 values. The hackathon version is anonymous; possession of the ID is required to access a report, and reports expire after 24 hours.

| Method and path | Purpose | Success response |
|---|---|---|
| `POST /api/v1/analyses` | Upload and validate the project archive. | `201` with analysis ID, discovered migrations, and evidence readiness. |
| `POST /api/v1/analyses/{id}/run` | Run static analysis and supported simulations synchronously. | `200` with verdict and evidence summary. |
| `GET /api/v1/analyses/{id}` | Retrieve status, findings, and evidence. | `200` analysis detail. |
| `GET /api/v1/analyses/{id}/timeline` | Retrieve ordered simulation events. | `200` timeline event array. |
| `POST /api/v1/analyses/{id}/plans` | Generate an unverified recovery plan. | `201` plan ID and candidate plan. |
| `POST /api/v1/analyses/{id}/plans/{plan_id}/verify` | Verify a plan from a clean baseline. | `200` verification result and updated report verdict. |
| `GET /api/v1/analyses/{id}/report` | Retrieve the sanitized final report. | `200` report document payload. |
| `DELETE /api/v1/analyses/{id}` | Delete report metadata immediately. | `204`. |

### 12.1 Create-analysis request

`multipart/form-data` fields:

- `project_bundle`: required `.zip` file.
- `candidate_migration`: required migration-folder basename.
- `use_demo`: optional boolean; mutually exclusive with `project_bundle`.

### 12.2 Core Pydantic types

- `ArtifactManifest`
- `MigrationArtifact`
- `RiskFinding`
- `EvidenceDimension`
- `SimulationRun`
- `LegacyQueryResult`
- `SnapshotSummary`
- `RecoveryAssessment`
- `RecoveryPlan`
- `PlanPhase`
- `VerificationResult`
- `TimelineEvent`
- `AnalysisSummary`
- `EvidenceReport`

### 12.3 Error model

```json
{
  "error": {
    "code": "UNSUPPORTED_SQL",
    "message": "The candidate contains a server-level operation that cannot run in verified mode.",
    "analysis_id": "opaque-uuid",
    "details": {
      "statement_index": 3,
      "category": "server_administration"
    }
  }
}
```

Messages must be safe for users; stack traces, filesystem paths, credentials, and raw fixture values remain server-side and are sanitized before logging.

---

## 13. Persistence model

Cloud SQL stores sanitized metadata in the existing `app` schema through SQLAlchemy 2 and Alembic.

| Entity | Key fields | Retention |
|---|---|---|
| `Analysis` | ID, status, evidence level, verdict, input hash, provider, candidate name, expiry timestamps | 24 hours for anonymous MVP |
| `RiskFinding` | Analysis ID, severity, category, statement index, sanitized reason | With analysis |
| `SimulationRun` | Analysis ID, run type, boundary, status, durations, recovery class | With analysis |
| `LegacyQueryResult` | Analysis ID, query name/hash, outcome, sanitized error, affected-row count | With analysis |
| `TimelineEvent` | Analysis ID, sequence, event type, status, sanitized message | With analysis |
| `RecoveryPlan` | Analysis ID, model/version, strategy, structured generated plan, verification state | With analysis |
| `VerificationResult` | Plan ID, evidence dimensions, verdict, completion timestamp | With analysis |

Raw archives, extracted migration files, fixture SQL, fixture rows, legacy-query SQL, PostgreSQL data directories, and model prompts are temporary artifacts. They are deleted when the analysis reaches a terminal state, after plan verification, or when the lifecycle timeout expires.

The Phase 1 schema adds `User`, Clerk identity, ownership, configurable retention, and persistent history. No incomplete user table should be deployed in the MVP merely because an unfinished model already exists.

---

## 14. Security, privacy, and abuse resistance

### 14.1 Trust boundaries

Uploads, SQL, archives, fixture values, query contracts, and model output are untrusted. The FastAPI process is a control plane; the PostgreSQL process is a disposable execution sandbox; Gemini is an untrusted planner; Cloud SQL is the sanitized evidence store.

### 14.2 Required controls

- Never accept a database URL or production credentials.
- Normalize archive paths before extraction and reject traversal and symlinks.
- Verify compressed/uncompressed limits before and during extraction.
- Use a unique temporary directory with restrictive permissions.
- Run PostgreSQL and migration SQL as non-root/non-superuser identities.
- Use Unix sockets and disable external sandbox listeners.
- Apply statement, lock, process, memory, disk, and lifecycle timeouts.
- Block server-level, filesystem, network, privilege, and untrusted-language SQL.
- Redact literals before Gemini prompts and persistent logging.
- Store artifact hashes instead of artifacts.
- Remove sandbox directories after every terminal path and on service startup.
- Do not log environment variables, authorization headers, SQL literals, or fixture rows.
- Require deterministic verification after every model-generated plan.

### 14.3 Residual risk

PostgreSQL is a complex execution engine and SQL policy enforcement can contain gaps. The MVP is safe for controlled synthetic demonstrations, not hostile multi-tenant internet exposure. Production rollout requires stronger process/container isolation, egress denial, independent workers, quotas, fuzzing, and security review.

---

## 15. Non-functional requirements

### 15.1 Performance and limits

- Archive validation: under 3 seconds at the maximum MVP input size.
- Static analysis: under 5 seconds for 25 statements.
- Each simulation stage: under 90 seconds.
- Gemini plan generation: target under 30 seconds; fail closed on timeout.
- One active simulation per instance; additional requests receive an explicit busy/queued response.
- Report endpoints: target P95 under 500 ms after persistence.

### 15.2 Reliability

- Every simulation starts from a new or restored clean baseline.
- A failed stage cannot reuse a contaminated sandbox for positive verification.
- Timeline events are ordered and append-only within an analysis.
- Terminal cleanup is idempotent.
- Expired reports return `410 Gone`; unknown IDs return `404 Not Found`.

### 15.3 Observability

Emit structured logs and metrics for stage duration, failure category, cleanup success, sandbox startup, model latency, model schema failure, and final evidence level. Logs use analysis IDs but no raw SQL literals or fixture values.

### 15.4 Accessibility and usability

- All risk states use text and icons in addition to color.
- The stepper is keyboard navigable and screen-reader labeled.
- Code blocks support horizontal scrolling and copy actions.
- Findings link to statement indexes and evidence events.
- Loading, empty, error, timeout, unsupported, and expired states are explicit.

---

## 16. Deployment architecture and prerequisites

### 16.1 Existing foundation to preserve

- Next.js frontend on Cloud Run.
- FastAPI backend on Cloud Run and GKE.
- Cloud SQL PostgreSQL 18 for application metadata.
- GKE Autopilot, Artifact Registry, Workload Identity, and CI/CD foundations.

No existing Cloud SQL, GKE, Cloud Run, Artifact Registry, IAM, or networking resource is rebuilt by default.

### 16.2 MVP implementation status

Completed in the release-candidate branches:

- Repaired the backend baseline and added green pull-request CI.
- Added SQLAlchemy domain models and Alembic migrations for sanitized report metadata and optional Clerk ownership.
- Packaged PostgreSQL 18 server binaries in the backend simulator image.
- Increased simulator memory and ephemeral-storage limits and provided a bounded writable `/tmp` volume.
- Gated the synchronous MVP to one simulator and one control instance, with active-analysis and creation-rate limits.
- Enabled `aiplatform.googleapis.com` and granted the runtime identity `roles/aiplatform.user`.
- Added explicit stable-model Gemini configuration without repository credentials.
- Routed browser uploads through the Next.js server proxy and forwarded only the generated Clerk bearer token when authentication is configured.

Pending operational action: merge and deploy the release-candidate pull requests after explicit approval. Clerk credentials must be configured atomically on both services before changing `CLERK_AUTH_REQUIRED` from `false`.

Production architecture replaces in-process sandbox management with an asynchronous, isolated worker/job per analysis.

---

## 17. Solo six-hour delivery plan

The sequence is evidence-first. If a milestone overruns, cut visual breadth before cutting deterministic verification.

| Time | Deliverable | Exit condition |
|---|---|---|
| 0:00-0:30 | Repair baseline | Ruff, compile, and existing tests pass; incomplete Clerk code no longer blocks CI. |
| 0:30-1:20 | Domain and upload validation | Demo archive stages safely; domain migration and core Pydantic contracts exist. |
| 1:20-2:20 | Risk engine and history replay | Four rule families produce deterministic findings; pre-candidate schema reconstructs. |
| 2:20-3:20 | PostgreSQL simulation | Fixtures, candidate, integrity checks, and legacy queries execute in disposable Postgres. |
| 3:20-4:05 | Failure and recovery | Statement boundaries, retry outcomes, and recovery classes appear in evidence. |
| 4:05-4:45 | Gemini plan and verification | Structured candidate plan is policy-gated and rerun from clean baseline. |
| 4:45-5:30 | Compact frontend | Six-stage stepper renders the built-in demo and live report data. |
| 5:30-6:00 | Verification and rehearsal | Backend/frontend tests pass; live demo completes; limitations are visible. |

Deferred immediately when time is at risk:

- Clerk authentication and user history.
- CSV fixture mapping.
- Asynchronous workers and live event streaming.
- Report PDF export.
- GitHub pull-request integration.
- User-authored policy configuration.

---

## 18. Test and acceptance plan

### 18.1 Deterministic rule tests

- A nullable column addition without a default passes destructive/data checks.
- `DROP COLUMN` produces a critical data-loss finding.
- `TRUNCATE` produces a critical destructive finding.
- A required column without default against populated fixtures fails.
- Duplicate fixture values block a unique constraint.
- Historical invalid values block a check constraint.
- A removed or renamed column breaks a matching legacy query.
- Lock-prone statements are labeled heuristic rather than measured.

### 18.2 PostgreSQL integration tests

- Complete prior migration history replays in lexical order.
- Provider mismatch and missing migration files fail validation.
- Candidate success produces the expected schema diff.
- Fixture counts and checksums remain stable where preservation is expected.
- Failure before statement 1 leaves baseline unchanged.
- Failure after each statement records partial state.
- Explicit transaction failure rolls back and is distinguished from committed partial state.
- Retry succeeds only when the resulting state matches the target.
- Destructive schema reversal is classified as data-irreversible when values are lost.
- Every run cleans its data directory after success, error, and timeout.

### 18.3 AI plan tests

- Valid Gemini JSON parses into `RecoveryPlan`.
- Invalid JSON receives one repair attempt and then deterministic fallback.
- Schema-valid but forbidden SQL is rejected.
- Schema-valid but semantically failing SQL is rejected by fresh simulation.
- No plan receives `VERIFIED_FOR_REVIEW` before verification completes.
- Prompt payload contains no fixture values or raw legacy-query literals.

### 18.4 Security tests

- Zip path traversal, absolute paths, symlinks, duplicate normalized paths, nested archives, and zip bombs are rejected.
- Oversized archives and statement/query counts are rejected.
- Role/database administration and `COPY ... PROGRAM` are rejected.
- Long-running statements hit timeouts.
- Logs and persisted rows contain no credentials, raw fixtures, or authorization tokens.
- Expired and deleted analysis IDs cannot retrieve reports.

### 18.5 Frontend tests

- Upload, candidate selection, static-only, running, unsafe, conditional, verified, error, timeout, unsupported, and expired states render correctly.
- Risk cards and timelines remain understandable without color.
- The built-in demo completes upload through report.
- Original and generated-plan verdicts cannot be confused.
- Production execution is always displayed as not performed.

### 18.6 Release acceptance

The MVP is accepted only when:

1. The built-in unsafe migration is deterministically blocked.
2. The old application contract failure is visible.
3. At least one interruption scenario demonstrates unsafe retry.
4. Gemini produces or falls back to an expand-and-contract candidate.
5. The candidate plan passes a clean PostgreSQL verification.
6. The final report says `VERIFIED_FOR_REVIEW`, not “safe to deploy.”
7. No production database connection exists anywhere in the flow.
8. All automated checks pass and the live demo completes in under five minutes.

---

## 19. Demo script

1. Open RollbackReady and select **Load unsafe phone migration**.
2. Show the archive contents and the selected candidate migration.
3. Start analysis and move through the schema comparison.
4. Show the critical finding: required `phone` column against three existing users.
5. Show normal migration failure and the old registration-query failure.
6. Open the failure lab and show the statement-boundary recovery matrix.
7. Request a safer plan and emphasize that it is initially unverified.
8. Show the expand, backfill, verify, and contract phases.
9. Verify the plan in a clean PostgreSQL sandbox.
10. Open the final report and read the evidence dimensions.
11. Close with: “AI proposed the plan; deterministic PostgreSQL execution earned the verdict.”

---

## 20. Roadmap

### Phase 1: Authenticated developer workspace

- Configure production Clerk credentials and enable the implemented owner-isolation path.
- Add account-facing long-term history and team workspace views.
- Add configurable deletion and retention.
- Add CSV fixtures with explicit table/column mapping.
- Add asynchronous analysis workers and progress polling/streaming.
- Add report export and rerun support.
- Harden sandbox quotas and failure cleanup.

### Phase 2: Pull-request and team workflow

- GitHub App and GitHub Action integration.
- Pull-request annotations and configurable blocking thresholds.
- Team workspaces, roles, policy sets, and audit history.
- Encrypted artifact storage with opt-in retention.
- Baseline caching keyed by immutable migration-history hashes.
- Policy waivers with owner, reason, expiry, and evidence.

### Phase 3: Production-grade simulation platform

- Dedicated isolated job/container per analysis with network egress denied.
- PostgreSQL version matrix and extension allowlists.
- Optional sanitized production-clone integrations with explicit consent.
- Scale-aware lock and rewrite estimation using catalog metadata.
- MySQL/MariaDB, SQL Server, CockroachDB, and SQLite adapters.
- Enterprise SSO, private networking, regional data residency, and compliance controls.

---

## 21. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| SQL parser misses PostgreSQL syntax | False confidence or unsupported execution | Fail closed; verified mode requires every statement to parse and pass policy. |
| Sandbox escape or resource abuse | Service compromise or denial of service | Synthetic controlled MVP only; unprivileged role/process, no network listener, quotas, timeouts, production worker isolation roadmap. |
| Fixture data is unrepresentative | Constraints pass while production fails | Show evidence bounds prominently; require user-owned synthetic production-shaped fixtures. |
| Legacy contract is incomplete | Rolling-deploy breakage not detected | Report query count and `NOT_TESTED` gaps; never infer complete application compatibility. |
| AI generates plausible but wrong SQL | Unsafe recommendation | Strict Pydantic schema, SQL policy, fresh deterministic simulation, no automatic deployment. |
| Small data hides lock behavior | Misleading operational claim | Report lock risk as heuristic only; do not report measured production duration. |
| Solo six-hour schedule overruns | Incomplete demo | Preserve built-in fixture and evidence pipeline; defer auth, history, CSV, streaming, and integrations. |
| Release candidate is not yet promoted | The live site remains on the earlier foundation revision | Keep draft PR checks green; merge and deploy only after explicit approval and run the full smoke script. |
| Clerk credentials are not configured | Optional sign-in remains disabled | Keep anonymous mode internally consistent; configure both services and verify session forwarding before requiring authentication. |

---

## 22. Locked assumptions

- Product name: RollbackReady.
- Database engine: PostgreSQL only for the MVP, using PostgreSQL 18 in the existing project.
- Primary experience: web upload and built-in demonstration.
- Primary persona: Prisma application teams.
- Safety claim: verified for human review, never safe to deploy.
- MVP access: anonymous opaque analysis IDs; Clerk deferred to Phase 1.
- Model: Vertex AI stable `gemini-3.6-flash`, configurable by explicit stable identifier.
- Raw artifact retention: lifecycle-only; sanitized anonymous reports expire after 24 hours.
- Production connections: prohibited.
- Execution: disposable local PostgreSQL cluster per analysis for the MVP; isolated worker per analysis in production.
- Lock results: heuristic only.
- Deployment and repository mutation: outside this PRD deliverable and require explicit approval.

---

## 23. References

1. Prisma, “prisma migrate deploy,” https://docs.prisma.io/docs/cli/migrate/deploy
2. Prisma, “About migration histories,” https://www.prisma.io/docs/orm/prisma-migrate/understanding-prisma-migrate/migration-histories
3. Atlas, “Migration analyzers,” https://atlasgo.io/lint/analyzers
4. Squawk, “Quick Start,” https://squawkhq.com/docs/
5. Prisma, “Deploying database changes with Prisma Migrate,” https://docs.prisma.io/docs/orm/prisma-client/deployment/deploy-database-changes-with-prisma-migrate
6. Google AI for Developers, “Gemini models,” https://ai.google.dev/gemini-api/docs/models
7. Google AI for Developers, “Structured outputs,” https://ai.google.dev/gemini-api/docs/structured-output
8. Prisma, “Expand-and-contract migrations,” https://www.prisma.io/docs/guides/database/data-migration
9. Prisma, “About the shadow database,” https://docs.prisma.io/docs/orm/prisma-migrate/understanding-prisma-migrate/shadow-database
