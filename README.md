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
[`../../docs/rollbackready-prd.md`](../../docs/rollbackready-prd.md). Never put
production data or a production connection string in an analysis bundle.

## Deployment

Pushes to `main` run `.github/workflows/deploy.yml`, test the Python application,
build an immutable container image, push it to Google Artifact Registry, apply
Alembic migrations, and deploy that exact image to Cloud Run and GKE Autopilot.
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

The source layout follows the earlier `nyc-r2-api` FastAPI project: core
infrastructure is isolated from routers, models, schemas, and services. Alembic
owns the schema history. Migration `0001_create_app_schema` creates the `app`
schema; it intentionally contains zero application tables until the product idea
and data model are selected.

The same immutable image is deployed to both Cloud Run and GKE Autopilot. GKE
uses Workload Identity to impersonate the existing backend runtime service
account, so neither environment stores a Google service-account key. Each push
to `main` runs Alembic first and then rolls out both hosted backends.

GKE keeps three backend Pods warm so abrupt connection bursts are distributed
immediately. The HPA targets 50% of requested CPU, can grow to 20 Pods, and waits
for ten minutes of lower demand before removing up to 50% of the Pods per minute.
Autopilot adds or removes the underlying compute capacity for those Pods. The
public Service uses a backend-service/NEG load balancer with pod-count-weighted
routing and the reserved regional address `nycr3s1-gke-backend-ip`.

## Structure

```text
app/
|-- core/       # Configuration, Cloud SQL connector, SQLAlchemy base
|-- models/     # Future SQLAlchemy models
|-- routers/    # FastAPI routes and health probes
|-- schemas/    # Future Pydantic request/response schemas
|-- services/   # Future business logic
`-- main.py     # FastAPI application assembly
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
