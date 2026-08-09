from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration supplied by Cloud Run environment variables."""

    instance_connection_name: str | None
    iam_database_user: str | None
    database_name: str | None
    database_url: str | None
    cors_origins: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> Settings:
        origins = tuple(
            origin.strip()
            for origin in os.getenv("CORS_ORIGIN", "").split(",")
            if origin.strip()
        )
        return cls(
            instance_connection_name=os.getenv("INSTANCE_CONNECTION_NAME"),
            iam_database_user=os.getenv("IAM_DB_USER"),
            database_name=os.getenv("DB_NAME"),
            database_url=os.getenv("DATABASE_URL"),
            cors_origins=origins,
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
