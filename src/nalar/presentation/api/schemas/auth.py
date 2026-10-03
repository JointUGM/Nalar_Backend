from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator

from nalar.presentation.api.schemas.common import Body


class LoginIn(Body):
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=256)


class ActivateIn(Body):
    activation_id: UUID
    token_hash: SecretStr = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=256)


class PasswordResetIn(Body):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class PasswordResetConfirmIn(Body):
    reset_id: UUID
    token_hash: SecretStr = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=256)


class PasswordChangeIn(Body):
    current_password: SecretStr = Field(min_length=1, max_length=256)
    new_password: SecretStr = Field(min_length=1, max_length=256)


class SessionOut(BaseModel):
    user_id: UUID
    expires_at: int
