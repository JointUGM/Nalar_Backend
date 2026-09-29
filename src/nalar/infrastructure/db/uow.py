from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Self

from asyncpg.transaction import Transaction

from nalar.application.ports.ai import AiInvocationLog
from nalar.application.ports.authz import Authz
from nalar.application.ports.identity import IdentityRepo
from nalar.application.ports.jobs import JobStore
from nalar.application.ports.participants import ParticipantsRepo
from nalar.application.ports.publications import PublicationsRepo
from nalar.application.ports.queue import QueueSender
from nalar.application.ports.runs import RunsRepo
from nalar.application.ports.sessions import SessionsRepo
from nalar.infrastructure.db.authz import PgAuthz
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.ai_invocations import PgAiInvocationLog
from nalar.infrastructure.db.repositories.identity import PgIdentityRepo
from nalar.infrastructure.db.repositories.jobs import PgJobStore
from nalar.infrastructure.db.repositories.participants import PgParticipantsRepo
from nalar.infrastructure.db.repositories.publications import PgPublicationsRepo
from nalar.infrastructure.db.repositories.runs import PgRunsRepo
from nalar.infrastructure.db.repositories.sessions import PgSessionsRepo
from nalar.infrastructure.queue.pgmq import PgmqSender

type AcquireConnection = Callable[[], AbstractAsyncContextManager[DbConnection]]


class PgUnitOfWork:
    authz: Authz
    ai_invocations: AiInvocationLog
    jobs: JobStore
    queue: QueueSender
    identity: IdentityRepo
    publications: PublicationsRepo
    runs: RunsRepo
    participants: ParticipantsRepo
    sessions: SessionsRepo

    def __init__(self, acquire: AcquireConnection) -> None:
        self._acquire = acquire
        self._lease: AbstractAsyncContextManager[DbConnection] | None = None
        self._transaction: Transaction | None = None

    async def __aenter__(self) -> Self:
        self._lease = self._acquire()
        conn = await self._lease.__aenter__()
        try:
            self._transaction = conn.transaction()
            await self._transaction.start()
        except BaseException as exc:
            # __aexit__ never runs when __aenter__ raises; without this the pool loses a slot.
            await self._lease.__aexit__(type(exc), exc, exc.__traceback__)
            self._lease = None
            self._transaction = None
            raise
        self.authz = PgAuthz(conn)
        self.ai_invocations = PgAiInvocationLog(conn)
        self.jobs = PgJobStore(conn)
        self.queue = PgmqSender(conn)
        self.identity = PgIdentityRepo(conn)
        self.publications = PgPublicationsRepo(conn)
        self.runs = PgRunsRepo(conn)
        self.participants = PgParticipantsRepo(conn)
        self.sessions = PgSessionsRepo(conn)
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
