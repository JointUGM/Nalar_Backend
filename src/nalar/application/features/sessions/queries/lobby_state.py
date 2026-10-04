from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import ParticipantStatus, RunStatus


@dataclass(frozen=True)
class LobbyState:
    run_status: RunStatus
    participant_status: ParticipantStatus
    warmup_choice_id: str | None
    session_id: UUID | None
    started_at: datetime | None
    deadline_at: datetime | None


class LobbyStateQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, run_id: UUID) -> LobbyState:
        async with self._uow:
            target = await self._uow.participants.run_target(run_id)
            if target is None or not await self._uow.authz.is_enrolled(actor_id, target.class_id):
                raise NotFound()
            participant = await self._uow.participants.get(run_id, actor_id)
            if participant is None or target is None:
                raise NotFound()
            session = (
                await self._uow.sessions.get_ref(participant.session_id)
                if participant.session_id
                else None
            )
        return LobbyState(
            run_status=target.status,
            participant_status=participant.status,
            warmup_choice_id=participant.warmup_choice_id,
            session_id=participant.session_id,
            started_at=session.started_at if session else None,
            deadline_at=session.deadline_at if session else None,
        )
