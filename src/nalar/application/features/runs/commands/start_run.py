from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.ports.background import BackgroundWork
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import RunStatus
from nalar.domain.runs import can_start
from nalar.domain.sessions import deadline_at


@dataclass(frozen=True)
class StartRun:
    actor_id: UUID
    run_id: UUID


@dataclass(frozen=True)
class RunStarted:
    started_at: datetime
    started_count: int


class StartRunHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock, background: BackgroundWork) -> None:
        self._uow = uow
        self._clock = clock
        self._background = background

    async def execute(self, cmd: StartRun) -> RunStarted:
        async with self._uow:
            if not await self._uow.authz.teaches_run(cmd.actor_id, cmd.run_id):
                raise NotFound()
            # Review Focus 4: a second Start waits on this lock, then sees the run already open.
            run = await self._uow.runs.lock(cmd.run_id)
            if run is None:
                raise NotFound()
            if run.status is RunStatus.open and run.started_at is not None:
                return RunStarted(run.started_at, await self._uow.runs.started_count(run.id))
            if not can_start(run.mode, run.status):
                raise Conflict("RUN_STATE_CONFLICT", details={"status": run.status.value})
            now = self._clock.now()
            count = await self._uow.runs.start(run, now, deadline_at(now, run.max_duration_minutes))
        self._background.warm_run(run.id)
        return RunStarted(now, count)
