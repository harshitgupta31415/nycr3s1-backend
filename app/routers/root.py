from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/")
async def service_identity() -> dict[str, str]:
    return {
        "service": "rollbackready-backend",
        "status": "ready",
        "runtime": "python-fastapi",
        "message": "RollbackReady analysis API is ready.",
    }
