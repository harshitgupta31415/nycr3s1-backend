from typing import Any

from fastapi import Depends, HTTPException, Request, status
from clerk_backend_api import Clerk
from clerk_backend_api.security.types import AuthenticateRequestOptions

from app.core.config import settings


# Create one Clerk SDK instance for the application.
clerk = Clerk(
    bearer_auth=settings.CLERK_SECRET_KEY,
)


async def get_current_user(request: Request) -> dict[str, Any]:
    """
    Authenticate the incoming FastAPI request using Clerk.

    Returns:
        Clerk session token payload.

    Raises:
        HTTPException(401) if the request is not authenticated.
    """

    try:
        request_state = clerk.authenticate_request(
            request,
            AuthenticateRequestOptions(
                authorized_parties=settings.authorized_parties,
                jwt_key=settings.CLERK_JWT_KEY,
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
    current_user: dict[str, Any] = Depends(get_current_user),
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