import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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


def request_with_token(token: str | None = None) -> Request:
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def signing_material() -> tuple[bytes, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem.decode()


def configured_settings(public_key: str) -> Settings:
    return replace(
        unconfigured_settings(),
        clerk_jwt_key=public_key,
        clerk_issuer="https://issuer.example",
        clerk_authorized_parties=("https://app.example",),
    )


def test_auth_module_imports_without_clerk_credentials() -> None:
    environment = os.environ.copy()
    environment["CLERK_JWT_KEY"] = ""
    environment["CLERK_ISSUER"] = ""
    result = subprocess.run(
        [sys.executable, "-c", "import app.core.clerk_auth"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_unconfigured_auth_returns_controlled_error(monkeypatch) -> None:
    monkeypatch.setattr(clerk_auth, "settings", unconfigured_settings())
    with pytest.raises(HTTPException) as raised:
        asyncio.run(clerk_auth.get_current_user(request_with_token()))
    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_configured_auth_rejects_unsigned_request(monkeypatch) -> None:
    _, public_key = signing_material()
    monkeypatch.setattr(clerk_auth, "settings", configured_settings(public_key))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(clerk_auth.get_current_user(request_with_token()))
    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_valid_clerk_session_jwt_is_accepted(monkeypatch) -> None:
    private_key, public_key = signing_material()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "user_test",
            "iss": "https://issuer.example",
            "azp": "https://app.example",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
    )
    monkeypatch.setattr(clerk_auth, "settings", configured_settings(public_key))
    payload = asyncio.run(clerk_auth.get_current_user(request_with_token(token)))
    assert payload["sub"] == "user_test"


def test_wrong_authorized_party_is_rejected(monkeypatch) -> None:
    private_key, public_key = signing_material()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "user_test",
            "iss": "https://issuer.example",
            "azp": "https://evil.example",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
    )
    monkeypatch.setattr(clerk_auth, "settings", configured_settings(public_key))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(clerk_auth.get_current_user(request_with_token(token)))
    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
