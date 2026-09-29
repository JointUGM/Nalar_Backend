from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Self

from asyncpg.transaction import Transaction

from nalar.application.ports.ai import AiInvocationLog
from nalar.application.ports.authz import Authz
from nalar.infrastructure.db.authz import PgAuthz
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.ai_invocations import PgAiInvocationLog

type AcquireConnection = Callable[[], AbstractAsyncContextManager[DbConnection]]


class PgUnitOfWork:
    authz: Authz
    ai_invocations: AiInvocationLog

    def __init__(self, acquire: AcquireConnection) -> None:
        self._acquire = acquire
        self._lease: AbstractAsyncContextManager[DbConnection] | None = None
        self._transaction: Transaction | None = None

    async def __aenter__(self) -> Self:
        self._lease = self._acquire()
        conn = await self._lease.__aenter__()
        self._transaction = conn.transaction()
        await self._transaction.start()
        self.authz = PgAuthz(conn)
        self.ai_invocations = PgAiInvocationLog(conn)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._lease is not None and self._transaction is not None
        try:
            if exc_type is None:
                await self._transaction.commit()
            else:
                await self._transaction.rollback()
        finally:
            await self._lease.__aexit__(exc_type, exc, tb)
            self._lease = None
            self._transaction = None
