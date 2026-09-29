from types import TracebackType
from typing import Protocol, Self

from nalar.application.ports.ai import AiInvocationLog
from nalar.application.ports.authz import Authz
from nalar.application.ports.identity import IdentityRepo
from nalar.application.ports.jobs import JobStore
from nalar.application.ports.publications import PublicationsRepo
from nalar.application.ports.queue import QueueSender
from nalar.application.ports.runs import RunsRepo


class UnitOfWork(Protocol):
    """One database transaction. Enqueued messages commit or roll back with it."""

    authz: Authz
    ai_invocations: AiInvocationLog
    jobs: JobStore
    queue: QueueSender
    identity: IdentityRepo
    publications: PublicationsRepo
    runs: RunsRepo

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
