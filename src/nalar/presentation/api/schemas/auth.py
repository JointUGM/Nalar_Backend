from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

from nalar.presentation.api.schemas.common import Body


class LoginIn(Body):
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=256)


class SessionOut(BaseModel):
    user_id: UUID
    expires_at: int
