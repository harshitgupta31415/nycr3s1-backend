from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Local overrides are loaded first. ``override=False`` keeps values supplied by
# the real process environment authoritative in Cloud Run, GKE, and CI.
load_dotenv(_PROJECT_ROOT / ".env.local", override=False)
load_dotenv(_PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration supplied by Cloud Run environment variables."""

    instance_connection_name: str | None
    iam_database_user: str | None
    database_name: str | None
    database_url: str | None
    cors_origins: tuple[str, ...]
    clerk_secret_key: str | None = None
    clerk_jwt_key: str | None = None
    clerk_authorized_parties: tuple[str, ...] = ()
    clerk_auth_required: bool = False
    google_cloud_project: str | None = None
    google_cloud_location: str = "global"
    google_genai_use_vertexai: bool = True
    gemini_model: str = "gemini-3.6-flash"
    rollbackready_sandbox_backend: str = "auto"
    rollbackready_postgres_bin: str | None = None
    rollbackready_postgres_image: str = "postgres:18-alpine"
    rollbackready_statement_timeout_ms: int = 5000
    rollbackready_lock_timeout_ms: int = 2000
    rollbackready_total_runtime_seconds: int = 90
    rollbackready_max_total_rows: int = 10_000
    rollbackready_max_active_analyses: int = 25
    rollbackready_create_rate_limit_per_minute: int = 10
    rollbackready_persist_reports: bool = False

    @classmethod
    def from_environment(cls) -> Settings:
        origins = tuple(
            origin.strip()
            for origin in os.getenv("CORS_ORIGIN", "").split(",")
            if origin.strip()
        )
        authorized_parties = tuple(
            party.strip()
            for party in os.getenv("CLERK_AUTHORIZED_PARTIES", "").split(",")
            if party.strip()
        )
        use_vertex_ai = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true").strip().lower()
        return cls(
            instance_connection_name=os.getenv("INSTANCE_CONNECTION_NAME"),
            iam_database_user=os.getenv("IAM_DB_USER"),
            database_name=os.getenv("DB_NAME"),
            database_url=os.getenv("DATABASE_URL"),
            cors_origins=origins,
            clerk_secret_key=os.getenv("CLERK_SECRET_KEY"),
            clerk_jwt_key=os.getenv("CLERK_JWT_KEY"),
            clerk_authorized_parties=authorized_parties,
            clerk_auth_required=os.getenv("CLERK_AUTH_REQUIRED", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
            google_genai_use_vertexai=use_vertex_ai in {"1", "true", "yes", "on"},
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
            rollbackready_sandbox_backend=os.getenv(
                "ROLLBACKREADY_SANDBOX_BACKEND", "auto"
            ).lower(),
            rollbackready_postgres_bin=os.getenv("ROLLBACKREADY_POSTGRES_BIN"),
            rollbackready_postgres_image=os.getenv(
                "ROLLBACKREADY_POSTGRES_IMAGE", "postgres:18-alpine"
            ),
            rollbackready_statement_timeout_ms=int(
                os.getenv("ROLLBACKREADY_STATEMENT_TIMEOUT_MS", "5000")
            ),
            rollbackready_lock_timeout_ms=int(
                os.getenv("ROLLBACKREADY_LOCK_TIMEOUT_MS", "2000")
            ),
            rollbackready_total_runtime_seconds=int(
                os.getenv("ROLLBACKREADY_TOTAL_RUNTIME_SECONDS", "90")
            ),
            rollbackready_max_total_rows=int(
                os.getenv("ROLLBACKREADY_MAX_TOTAL_ROWS", "10000")
            ),
            rollbackready_max_active_analyses=int(
                os.getenv("ROLLBACKREADY_MAX_ACTIVE_ANALYSES", "25")
            ),
            rollbackready_create_rate_limit_per_minute=int(
                os.getenv("ROLLBACKREADY_CREATE_RATE_LIMIT_PER_MINUTE", "10")
            ),
            rollbackready_persist_reports=os.getenv(
                "ROLLBACKREADY_PERSIST_REPORTS", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
        )

    @property
    def sync_database_url(self) -> str | None:
        """Normalize local Postgres URLs to the installed pg8000 driver."""
        if self.database_url is None:
            return None
        if self.database_url.startswith("postgresql+pg8000://"):
            return self.database_url
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace(
                "postgresql://", "postgresql+pg8000://", 1
            )
        if self.database_url.startswith("postgres://"):
            return self.database_url.replace("postgres://", "postgresql+pg8000://", 1)
        return self.database_url

    def require_database_configuration(self) -> tuple[str, str, str]:
        values = {
            "INSTANCE_CONNECTION_NAME": self.instance_connection_name,
            "IAM_DB_USER": self.iam_database_user,
            "DB_NAME": self.database_name,
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}"
            )

        return (
            self.instance_connection_name,
            self.iam_database_user,
            self.database_name,
        )


settings = Settings.from_environment()
