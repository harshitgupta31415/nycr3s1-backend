from __future__ import annotations

from threading import Lock
from typing import Any

from google.cloud.sql.connector import Connector, IPTypes
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """Base class for application models added after the product is defined."""

    metadata = MetaData(schema="app")


_connector: Connector | None = None
_engine: Engine | None = None
_engine_lock = Lock()


def _create_database_engine() -> Engine:
    global _connector

    engine_options = {
        # Match the three bounded simulation slots per instance. With the
        # regional 20-instance ceiling this caps the application at 60 pooled
        # metadata connections while simulations run in disposable local PG.
        "pool_size": 3,
        "max_overflow": 0,
        "pool_timeout": 10,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }

    if settings.sync_database_url:
        return create_engine(settings.sync_database_url, **engine_options)

    instance_connection_name, database_user, database_name = (
        settings.require_database_configuration()
    )
    _connector = Connector(refresh_strategy="LAZY")

    def get_connection():
        if _connector is None:
            raise RuntimeError("Cloud SQL connector is not initialized")

        return _connector.connect(
            instance_connection_name,
            "pg8000",
            user=database_user,
            db=database_name,
            enable_iam_auth=True,
            ip_type=IPTypes.PUBLIC,
            timeout=10,
        )

    return create_engine(
        "postgresql+pg8000://",
        creator=get_connection,
        **engine_options,
    )


def get_database_engine() -> Engine:
    """Create the shared Cloud SQL connection pool on first use."""
    global _engine

    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = _create_database_engine()

    return _engine


def check_database() -> dict[str, Any]:
    """Verify the hosted database connection and report its empty-state count."""
    with get_database_engine().connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT
                        current_database() AS database,
                        current_user AS database_user,
                        NOW() AS checked_at,
                        (
                            SELECT COUNT(*)::int
                            FROM information_schema.tables
                            WHERE table_schema = 'app'
                        ) AS application_table_count
                    """
                )
            )
            .mappings()
            .one()
        )

    return {
        "connected": True,
        "database": row["database"],
        "databaseUser": row["database_user"],
        "checkedAt": row["checked_at"],
        "applicationTableCount": row["application_table_count"],
    }


def close_database() -> None:
    """Release the SQLAlchemy pool and connector background resources."""
    global _connector, _engine

    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None

        if _connector is not None:
            _connector.close()
            _connector = None
