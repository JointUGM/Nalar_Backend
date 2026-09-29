from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class IngestTelemetry:
    actor_id: UUID
    session_id: UUID
    client_seq: int
    turn_index: int | None
    events_json: str
    client_sent_at: datetime | None


class IngestTelemetryHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, cmd: IngestTelemetry) -> int:
        async with self._uow:
            if not await self._uow.authz.owns_session(cmd.actor_id, cmd.session_id):
                raise NotFound()
            await self._uow.telemetry.insert(
                cmd.session_id, cmd.client_seq, cmd.turn_index, cmd.events_json, cmd.client_sent_at
            )
        return cmd.client_seq
