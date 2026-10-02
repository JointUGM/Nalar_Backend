from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class AuthAccount:
    id: UUID
    last_sign_in_at: datetime | None


class AuthAdmin(Protocol):
    async def create_or_find(self, email: str, full_name: str) -> AuthAccount:
        """Find or create an account without changing an existing user's password."""
        ...


class AuthAdminError(Exception):
    def __init__(self, *, retryable: bool) -> None:
        super().__init__("Auth account creation failed")
        self.retryable = retryable
