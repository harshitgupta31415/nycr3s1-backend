from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/")
async def service_identity() -> dict[str, str]:
    return {
        "service": "nycr3s1-backend",
        "status": "ready",
        "runtime": "python-fastapi",
        "message": "FastAPI backend and managed PostgreSQL foundation are ready.",
    }
