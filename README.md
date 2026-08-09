# RollbackReady backend

FastAPI control plane for evidence-based Prisma migration analysis. It validates
untrusted project archives, applies deterministic risk rules, runs disposable
PostgreSQL 18 simulations, orchestrates a constrained LangGraph recovery planner,
and persists only sanitized reports.

## Endpoints

- Cloud Run (stable HTTPS): `https://nycr3s1-backend-s2tvvhxdpa-el.a.run.app`
- GKE Autopilot load balancer: `http://34.100.156.113`

- `GET /` - service identity and readiness
- `GET /health` - health check
- `GET /health/ready` - database-backed readiness check
- `GET /health/database` - verifies the hosted PostgreSQL connection
- `GET /docs` - interactive Swagger API documentation
- `GET /openapi.json` - generated OpenAPI schema
- `POST /api/v1/analyses` - stage a ZIP bundle or the built-in demo
- `POST /api/v1/analyses/{id}/run` - run deterministic analysis and simulation
- `GET /api/v1/analyses/{id}` - retrieve the current analysis
- `GET /api/v1/analyses/{id}/timeline` - retrieve ordered evidence events
- `POST /api/v1/analyses/{id}/plans` - generate an unverified recovery plan
- `POST /api/v1/analyses/{id}/plans/{plan_id}/verify` - fresh-sandbox verification
- `GET /api/v1/analyses/{id}/report` - retrieve the sanitized report
- `DELETE /api/v1/analyses/{id}` - remove report metadata

## Local development

Python 3.13 is recommended.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --requirement requirements-dev.txt
python -m pytest
python -m app
```

The service listens on `PORT` or `8080` by default.

The default hackathon access mode is anonymous and uses possession of the
opaque analysis UUID as its boundary. When Clerk credentials are configured,
requests are associated with the authenticated Clerk subject and reports are
owner-isolated. Set `CLERK_AUTH_REQUIRED=true` only after the frontend session
integration is configured; otherwise the built-in demo remains available under
the reserved anonymous owner.

Set `ROLLBACKREADY_SANDBOX_BACKEND=docker` on Windows or macOS to use the
network-isolated `postgres:18-alpine` development sandbox. The deployed image
contains native PostgreSQL 18 binaries and listens only on a Unix socket.

Raw project bundles intentionally remain process-local and are deleted after
the analysis/verification lifecycle. A container or pod replacement can
therefore interrupt a staged or unverified analysis. The single-instance MVP
settings reduce routing risk but do not make the lifecycle restart-safe;
isolated asynchronous workers and encrypted temporary artifact storage remain
production-roadmap work.

The accepted ZIP layout is documented in
[`docs/rollbackready-prd.md`](docs/rollbackready-prd.md). Never put
production data or a production connection string in an analysis bundle.

## Deployment

Pushes to `main` run [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml),
test the Python application, build an immutable container image, push it to
Google Artifact Registry, apply Alembic migrations, and deploy that exact image
to Cloud Run and GKE Autopilot. The workflow verifies that the deployed OpenAPI
document identifies RollbackReady `0.3.0` and exposes the analysis API.
GitHub authenticates with short-lived Workload Identity Federation credentials;
no service-account key is stored in GitHub.

The service connects to Cloud SQL PostgreSQL through Google's Python Connector,
SQLAlchemy, and pg8000 with automatic IAM database authentication. The Cloud Run
runtime service account is the database identity, so no database password or
connection-string secret is required.

For optional local Docker development, `DATABASE_URL` can point to a local
PostgreSQL instance. Production does not set it and therefore always uses the
Cloud SQL IAM variables: `INSTANCE_CONNECTION_NAME`, `IAM_DB_USER`, and
`DB_NAME`.

Core infrastructure is isolated from routers, database models, API contracts,
and RollbackReady orchestration services. Alembic owns the schema history:
`0001` creates the application schema, while later revisions add sanitized
analysis evidence and optional Clerk ownership. Raw uploaded artifacts and
fixture values are never persisted in these tables.

The same immutable image is deployed to both Cloud Run and GKE Autopilot. GKE
uses Workload Identity to impersonate the existing backend runtime service
account, so neither environment stores a Google service-account key. Each push
to `main` runs Alembic first and then rolls out both hosted backends.

The synchronous MVP keeps exactly one backend control Pod because raw artifacts
remain process-local during the analysis lifecycle. The HPA is pinned to one
replica until Phase 1 moves simulations to isolated workers with durable,
encrypted temporary artifact storage. The public Service retains the reserved
regional address `nycr3s1-gke-backend-ip`.

## Structure

```text
app/
|-- core/           # Configuration, auth, Cloud SQL, SQLAlchemy base
|-- models/         # Sanitized evidence persistence models
|-- rollbackready/  # Contracts, rules, sandbox, simulation, planning, service
|-- routers/        # Analysis APIs and health probes
`-- main.py         # FastAPI application assembly and expiry lifecycle
alembic/        # Database migration history
k8s/            # GKE deployment, migrations, service, bootstrap, and autoscaling
tests/          # API tests
```

`k8s/database-bootstrap-job.yaml` is a secret-free, one-time operator manifest
for granting a new IAM database identity its initial PostgreSQL privileges. It is
not part of normal deployments and expects a temporary Kubernetes Secret named
`database-bootstrap`; that Secret must be deleted immediately after use.

Set `CORS_ORIGIN` on Cloud Run or GKE when the browser begins calling the API
directly from the Google Cloud frontend domain.
