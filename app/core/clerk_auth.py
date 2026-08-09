from __future__ import annotations

from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import settings


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
    """Verify a Clerk session JWT locally using its pinned public key."""
    key, issuer, authorized_parties = _configuration()
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(
            token.strip(),
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    authorized_party = payload.get("azp")
    if authorized_parties and authorized_party not in authorized_parties:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def get_clerk_user_id(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> str:
    user_id = current_user.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user ID is missing",
        )
    return user_id
