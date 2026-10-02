from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class AuthAccount:
    id: UUID
    last_sign_in_at: datetime | None
    email: str | None = None


class AuthAdmin(Protocol):
    async def get_account(self, user_id: UUID) -> AuthAccount | None: ...

    async def send_setup_email(self, email: str, redirect_to: str) -> None:
        """Submit a recovery email; ambiguous delivery raises an acceptance-unknown error."""
        ...

    async def create_or_find(self, email: str, full_name: str) -> AuthAccount:
        """Find or create an account without changing an existing user's password."""
        ...


class AuthAdminError(Exception):
    def __init__(self, *, retryable: bool) -> None:
        super().__init__("Auth account creation failed")
        self.retryable = retryable


class AuthEmailError(Exception):
    def __init__(
        self,
        category: str,
        *,
        retryable: bool = False,
        acceptance_unknown: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__("Auth email submission failed")
        self.category = category
        self.retryable = retryable
        self.acceptance_unknown = acceptance_unknown
        self.retry_after_s = retry_after_s
