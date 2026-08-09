from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

import app.main as main_module
from app.core.config import settings
from app.main import create_app


def database_result() -> dict:
    return {
        "connected": True,
        "database": "app",
        "databaseUser": "test-user",
        "checkedAt": datetime(2026, 8, 8, tzinfo=UTC),
        "applicationTableCount": 0,
    }


def test_root_identifies_fastapi_service() -> None:
    with TestClient(create_app(database_result)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "rollbackready-backend",
        "status": "ready",
        "runtime": "python-fastapi",
        "message": "RollbackReady analysis API is ready.",
    }


def test_service_health() -> None:
    with TestClient(create_app(database_result)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "rollbackready-backend",
    }


def test_database_health() -> None:
    with TestClient(create_app(database_result)) as client:
        response = client.get("/health/database")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["database"]["connected"] is True
    assert body["database"]["database"] == "app"
    assert body["database"]["applicationTableCount"] == 0


def test_database_readiness() -> None:
    with TestClient(create_app(database_result)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": {"connected": True},
    }


def test_database_health_failure() -> None:
    def unavailable_database() -> dict:
        raise RuntimeError("database unavailable")

    with TestClient(create_app(unavailable_database)) as client:
        response = client.get("/health/database")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "database": {"connected": False},
    }


def test_openapi_document_is_available() -> None:
    with TestClient(create_app(database_result)) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    document = response.json()
    assert document["info"]["title"] == "RollbackReady API"
    assert "/api/v1/analyses" in document["paths"]


def test_production_frontend_cors_preflight(monkeypatch) -> None:
    origin = "https://dbsentinal.get200.qd.je"
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(settings, cors_origins=(origin,)),
    )

    with TestClient(create_app(database_result)) as client:
        response = client.options(
            "/api/v1/analyses",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "POST" in response.headers["access-control-allow-methods"]
