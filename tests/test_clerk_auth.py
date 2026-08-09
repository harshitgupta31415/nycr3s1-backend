import asyncio
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from fastapi import HTTPException, Request, status

from app.core import clerk_auth
from app.core.config import Settings


def unconfigured_settings() -> Settings:
    return Settings(
        instance_connection_name=None,
        iam_database_user=None,
        database_name=None,
        database_url=None,
        cors_origins=(),
    )


def test_auth_module_imports_without_clerk_credentials() -> None:
    environment = os.environ.copy()
    environment["CLERK_SECRET_KEY"] = ""

    result = subprocess.run(
        [sys.executable, "-c", "import app.core.clerk_auth"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_unconfigured_auth_dependency_uses_anonymous_hackathon_identity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(clerk_auth, "settings", unconfigured_settings())
    monkeypatch.setattr(clerk_auth, "_clerk", None)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    payload = asyncio.run(clerk_auth.get_current_user(request))

    assert payload == {
        "sub": clerk_auth.ANONYMOUS_USER_ID,
        "auth_mode": "anonymous",
    }


def test_required_but_unconfigured_auth_returns_controlled_error(monkeypatch) -> None:
    monkeypatch.setattr(
        clerk_auth,
        "settings",
        replace(unconfigured_settings(), clerk_auth_required=True),
    )
    monkeypatch.setattr(clerk_auth, "_clerk", None)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    with pytest.raises(HTTPException) as raised:
        asyncio.run(clerk_auth.get_current_user(request))

    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert raised.value.detail == "Authentication is required but not configured"


def test_optional_configured_auth_allows_unsigned_anonymous_request(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        clerk_auth,
        "settings",
        replace(unconfigured_settings(), clerk_secret_key="configured-secret"),
    )
    monkeypatch.setattr(clerk_auth, "_clerk", None)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    payload = asyncio.run(clerk_auth.get_current_user(request))

    assert payload["sub"] == clerk_auth.ANONYMOUS_USER_ID
