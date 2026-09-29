from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class AuthUser:
    id: UUID


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> AuthUser:
        """Return the token's user, or raise Unauthenticated."""
        ...
