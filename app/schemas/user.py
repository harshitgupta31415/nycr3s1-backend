from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserResponse(BaseModel):
    id: int
    clerk_user_id: str
    email: str | None
    first_name: str | None
    last_name: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
    )


class UserUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None