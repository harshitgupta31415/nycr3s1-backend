from typing import Annotated, Any

from clerk_backend_api import Clerk
from clerk_backend_api.security.types import AuthenticateRequestOptions
from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings

_clerk: Clerk | None = None
ANONYMOUS_USER_ID = "__anonymous__"


def _get_clerk() -> Clerk:
    """Return a lazily initialized Clerk client when authentication is configured."""
    global _clerk

    if not settings.clerk_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        )

    if _clerk is None:
        _clerk = Clerk(bearer_auth=settings.clerk_secret_key)

    return _clerk


async def get_current_user(request: Request) -> dict[str, Any]:
    """
    Authenticate the incoming FastAPI request using Clerk.

    Returns:
        Clerk session token payload.

    Raises:
        HTTPException(503) if Clerk authentication is not configured.
        HTTPException(401) if the request is not authenticated.
    """

    if not settings.clerk_secret_key:
        if settings.clerk_auth_required:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication is required but not configured",
            )
        # The hackathon mode intentionally uses possession of an opaque UUID as
        # its access boundary. Keeping a reserved owner value lets the same
        # service and persistence code upgrade to Clerk without breaking that
        # anonymous flow.
        return {"sub": ANONYMOUS_USER_ID, "auth_mode": "anonymous"}

    if not settings.clerk_auth_required and not _has_auth_material(request):
        return {"sub": ANONYMOUS_USER_ID, "auth_mode": "anonymous"}

    try:
        request_state = _get_clerk().authenticate_request(
            request,
            AuthenticateRequestOptions(
                authorized_parties=list(settings.clerk_authorized_parties) or None,
                jwt_key=settings.clerk_jwt_key,
            ),
        )

        if not request_state.is_signed_in:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        payload = request_state.payload

        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return payload

    except HTTPException:
        raise

    except Exception as exc:
        # Do not expose Clerk/internal errors to clients.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_clerk_user_id(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> str:
    """
    Return the authenticated Clerk user ID.
    """

    user_id = current_user.get("sub")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user ID is missing",
        )

    return user_id


def _has_auth_material(request: Request) -> bool:
    if request.headers.get("authorization"):
        return True
    cookie = request.headers.get("cookie", "")
    return any(
        marker in cookie
        for marker in ("__session=", "__client_uat=", "__clerk")
    )
