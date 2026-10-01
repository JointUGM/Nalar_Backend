from typing import Protocol
from uuid import UUID


class AuthAdmin(Protocol):
    async def create_or_find(self, email: str, full_name: str) -> UUID:
        """Find or create an account with an unguessable password; activation is Tier 3."""
        ...


class AuthAdminError(Exception):
    def __init__(self, *, retryable: bool) -> None:
        super().__init__("Auth account creation failed")
        self.retryable = retryable
