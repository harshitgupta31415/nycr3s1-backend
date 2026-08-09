from datetime import UTC, datetime

from fastapi.testclient import TestClient

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
        "service": "nycr3s1-backend",
        "status": "ready",
        "runtime": "python-fastapi",
        "message": "FastAPI backend and managed PostgreSQL foundation are ready.",
    }


def test_service_health() -> None:
    with TestClient(create_app(database_result)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "nycr3s1-backend",
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
    assert response.json()["info"]["title"] == "NYCR3S1 Backend"
