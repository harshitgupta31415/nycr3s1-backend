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
    clerk_issuer: str | None = None
    clerk_authorized_parties: tuple[str, ...] = ()
    clerk_auth_mode: str = "anonymous_demo"
    google_cloud_project: str | None = None
    google_cloud_location: str = "global"
    gemini_api_key: str | None = None
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
    rollbackready_plan_rate_limit_per_hour: int = 3
    rollbackready_verify_rate_limit_per_hour: int = 5
    rollbackready_max_plans_per_analysis: int = 3
    rollbackready_max_unfinished_per_user: int = 5
    rollbackready_privileged_clerk_user_ids: frozenset[str] = frozenset()
    rollbackready_persist_reports: bool = False
    rollbackready_artifact_bucket: str | None = None
    rollbackready_artifact_retention_hours: int = 24

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
        privileged_clerk_user_ids = frozenset(
            user_id.strip()
            for user_id in os.getenv(
                "ROLLBACKREADY_PRIVILEGED_CLERK_USER_IDS", ""
            ).split(",")
            if user_id.strip()
        )
        use_vertex_ai = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true").strip().lower()
        legacy_auth_required = os.getenv("CLERK_AUTH_REQUIRED", "false").strip().lower()
        auth_mode = os.getenv("CLERK_AUTH_MODE")
        if auth_mode is None:
            auth_mode = (
                "required"
                if legacy_auth_required in {"1", "true", "yes", "on"}
                else "anonymous_demo"
            )
        auth_mode = auth_mode.strip().lower()
        if auth_mode not in {"required", "anonymous_demo"}:
            raise RuntimeError(
                "CLERK_AUTH_MODE must be either 'required' or 'anonymous_demo'."
            )
        configured = cls(
            instance_connection_name=os.getenv("INSTANCE_CONNECTION_NAME"),
            iam_database_user=os.getenv("IAM_DB_USER"),
            database_name=os.getenv("DB_NAME"),
            database_url=os.getenv("DATABASE_URL"),
            cors_origins=origins,
            clerk_secret_key=os.getenv("CLERK_SECRET_KEY"),
            clerk_jwt_key=os.getenv("CLERK_JWT_KEY"),
            clerk_issuer=os.getenv("CLERK_ISSUER"),
            clerk_authorized_parties=authorized_parties,
            clerk_auth_mode=auth_mode,
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
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
            rollbackready_plan_rate_limit_per_hour=int(
                os.getenv("ROLLBACKREADY_PLAN_RATE_LIMIT_PER_HOUR", "3")
            ),
            rollbackready_verify_rate_limit_per_hour=int(
                os.getenv("ROLLBACKREADY_VERIFY_RATE_LIMIT_PER_HOUR", "5")
            ),
            rollbackready_max_plans_per_analysis=int(
                os.getenv("ROLLBACKREADY_MAX_PLANS_PER_ANALYSIS", "3")
            ),
            rollbackready_max_unfinished_per_user=int(
                os.getenv("ROLLBACKREADY_MAX_UNFINISHED_PER_USER", "5")
            ),
            rollbackready_privileged_clerk_user_ids=privileged_clerk_user_ids,
            rollbackready_persist_reports=os.getenv(
                "ROLLBACKREADY_PERSIST_REPORTS", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            rollbackready_artifact_bucket=os.getenv("ROLLBACKREADY_ARTIFACT_BUCKET"),
            rollbackready_artifact_retention_hours=int(
                os.getenv("ROLLBACKREADY_ARTIFACT_RETENTION_HOURS", "24")
            ),
        )
        configured.validate_auth_configuration()
        return configured

    @property
    def clerk_auth_required(self) -> bool:
        return self.clerk_auth_mode == "required"

    def is_privileged_clerk_user(self, clerk_user_id: str) -> bool:
        """Return whether an authenticated account bypasses usage quotas."""
        return clerk_user_id in self.rollbackready_privileged_clerk_user_ids

    def validate_auth_configuration(self) -> None:
        if not self.clerk_auth_required:
            return
        missing: list[str] = []
        if not self.clerk_jwt_key:
            missing.append("CLERK_JWT_KEY")
        if not self.clerk_issuer:
            missing.append("CLERK_ISSUER")
        if not self.clerk_authorized_parties:
            missing.append("CLERK_AUTHORIZED_PARTIES")
        if missing:
            raise RuntimeError(
                "Clerk required mode is missing: " + ", ".join(missing)
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
