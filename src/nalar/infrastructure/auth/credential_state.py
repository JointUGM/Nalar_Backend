from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from nalar.application.errors import DependencyUnavailable
from nalar.application.ports.password_resets import CredentialSnapshot
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.password_resets import credential_snapshot, login_revision


class PgCredentialState:
    def __init__(self, acquire: Callable[[], AbstractAsyncContextManager[DbConnection]]) -> None:
        self._acquire = acquire

    async def snapshot(self, user_id: UUID) -> CredentialSnapshot:
        try:
            async with self._acquire() as conn:
                return await credential_snapshot(conn, user_id)
        except Exception as exc:
            raise DependencyUnavailable() from exc

    async def login_revision(self, email: str) -> int:
        try:
            async with self._acquire() as conn:
                return await login_revision(conn, email)
        except Exception as exc:
            raise DependencyUnavailable() from exc
