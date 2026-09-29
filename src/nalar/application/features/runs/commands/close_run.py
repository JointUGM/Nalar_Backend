from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import RunStatus
from nalar.domain.runs import can_close


@dataclass(frozen=True)
class CloseRun:
    actor_id: UUID
    run_id: UUID


@dataclass(frozen=True)
class RunClosed:
    closed_at: datetime


class CloseRunHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, cmd: CloseRun) -> RunClosed:
        async with self._uow:
            if not await self._uow.authz.teaches_run(cmd.actor_id, cmd.run_id):
                raise NotFound()
            run = await self._uow.runs.lock(cmd.run_id)
            if run is None:
                raise NotFound()
            if run.status is RunStatus.closed and run.closed_at is not None:
                return RunClosed(run.closed_at)
            if not can_close(run.status):
                raise Conflict("RUN_STATE_CONFLICT", details={"status": run.status.value})
            now = self._clock.now()
            await self._uow.runs.close(run.id, now)
        return RunClosed(now)
