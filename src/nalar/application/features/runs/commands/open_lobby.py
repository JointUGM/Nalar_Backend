from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, DependencyUnavailable, NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.domain import join_code
from nalar.domain.labels import RunStatus
from nalar.domain.runs import can_open_lobby

_CODE_ATTEMPTS = 5


class JoinCodes:
    def next(self) -> str:
        return join_code.generate()


@dataclass(frozen=True)
class OpenLobby:
    actor_id: UUID
    run_id: UUID


@dataclass(frozen=True)
class LobbyOpened:
    join_code: str
    lobby_opened_at: datetime


class OpenLobbyHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock, codes: JoinCodes) -> None:
        self._uow = uow
        self._clock = clock
        self._codes = codes

    async def execute(self, cmd: OpenLobby) -> LobbyOpened:
        async with self._uow:
            if not await self._uow.authz.teaches_run(cmd.actor_id, cmd.run_id):
                raise NotFound()
            run = await self._uow.runs.lock(cmd.run_id)
            if run is None:
                raise NotFound()
            if run.status is RunStatus.lobby and run.join_code and run.lobby_opened_at:
                return LobbyOpened(run.join_code, run.lobby_opened_at)
            if not can_open_lobby(run.mode, run.status):
                raise Conflict("RUN_STATE_CONFLICT", details={"status": run.status.value})
            now = self._clock.now()
            for _ in range(_CODE_ATTEMPTS):
                code = self._codes.next()
                if await self._uow.runs.open_lobby(run.id, code, now):
                    return LobbyOpened(code, now)
        raise DependencyUnavailable()
