from types import TracebackType
from typing import Protocol, Self

from nalar.application.ports.ai import AiInvocationLog
from nalar.application.ports.authz import Authz
from nalar.application.ports.jobs import JobStore
from nalar.application.ports.queue import QueueSender


class UnitOfWork(Protocol):
    """One database transaction. Enqueued messages commit or roll back with it."""

    authz: Authz
    ai_invocations: AiInvocationLog
    jobs: JobStore
    queue: QueueSender

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
