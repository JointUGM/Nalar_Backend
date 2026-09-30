from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class AuthUser:
    id: UUID
    session_id: UUID | None = None


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> AuthUser:
        """Return the token's user, or raise Unauthenticated."""
        ...


@dataclass(frozen=True)
class AuthTokens:
    user_id: UUID
    access_token: str
    refresh_token: str
    expires_at: int


class IdentityProvider(Protocol):
    async def sign_in(self, email: str, password: str) -> AuthTokens:
        """Exchange a password for tokens, or raise InvalidCredentials."""
        ...

    async def refresh(self, refresh_token: str) -> AuthTokens:
        """Rotate a refresh token, or raise Unauthenticated when it is no longer valid."""
        ...

    async def sign_out(self, access_token: str) -> None:
        """Revoke the token's session in the auth server; an invalid token is not an error."""
        ...


class SessionRevocations(Protocol):
    async def revoke(self, session_id: UUID) -> None:
        """Reject every access token of this session until the longest one could expire."""
        ...

    async def is_revoked(self, session_id: UUID) -> bool:
        """True only when a revocation is recorded; an unreachable store answers False."""
        ...


class LoginAttempts(Protocol):
    async def allow(self, email: str) -> bool:
        """Count one attempt; False past the per-minute limit, True if the store is unreachable."""
        ...
