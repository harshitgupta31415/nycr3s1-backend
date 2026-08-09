from __future__ import annotations

import re
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import settings

_USER_ID = re.compile(r"^user_[A-Za-z0-9]+$")
ANONYMOUS_USER_ID = "__anonymous__"


def _unauthorized(detail: str = "Authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _configuration() -> tuple[str, str, tuple[str, ...]]:
    if not settings.clerk_jwt_key or not settings.clerk_issuer:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is required but not configured",
        )
    key = settings.clerk_jwt_key.replace("\\n", "\n").strip()
    issuer = settings.clerk_issuer.rstrip("/")
    return key, issuer, settings.clerk_authorized_parties


async def get_current_user(request: Request) -> dict[str, Any]:
    """Return an anonymous demo principal or verify a Clerk JWT locally."""
    if settings.clerk_auth_mode == "anonymous_demo":
        return {
            "sub": ANONYMOUS_USER_ID,
            "auth_mode": "anonymous_demo",
            "org_id": None,
            "org_role": None,
        }

    key, issuer, authorized_parties = _configuration()
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized()
    try:
        payload = jwt.decode(
            token.strip(),
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except ExpiredSignatureError as exc:
        raise _unauthorized("Authentication token expired") from exc
    except InvalidTokenError as exc:
        raise _unauthorized("Invalid authentication token") from exc

    authorized_party = payload.get("azp")
    if authorized_parties and authorized_party not in authorized_parties:
        raise _unauthorized("Invalid authentication token")
    subject = payload.get("sub")
    if not isinstance(subject, str) or not _USER_ID.fullmatch(subject):
        raise _unauthorized("Invalid authentication subject")
    payload["auth_mode"] = "required"
    payload["org_id"] = payload.get("org_id")
    payload["org_role"] = payload.get("org_role")
    return payload


def get_clerk_user_id(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> str:
    user_id = current_user.get("sub")
    if not isinstance(user_id, str):
        raise _unauthorized("Authenticated user ID is missing")
    if user_id != ANONYMOUS_USER_ID and not _USER_ID.fullmatch(user_id):
        raise _unauthorized("Invalid authentication subject")
    return user_id
