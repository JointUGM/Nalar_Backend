from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, InvalidInput, NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import ParticipantStatus, RunStatus


@dataclass(frozen=True)
class SubmitWarmup:
    actor_id: UUID
    run_id: UUID
    choice_id: str


@dataclass(frozen=True)
class WarmupSaved:
    choice_id: str
    submitted_at: datetime


class SubmitWarmupHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, cmd: SubmitWarmup) -> WarmupSaved:
        async with self._uow:
            # The lookup is keyed by the caller, so a student only ever reaches their own row.
            participant = await self._uow.participants.get(cmd.run_id, cmd.actor_id)
            target = await self._uow.participants.run_target(cmd.run_id)
            if participant is None or target is None:
                raise NotFound()
            if participant.warmup_choice_id is not None and participant.warmup_submitted_at:
                if participant.warmup_choice_id == cmd.choice_id:
                    return WarmupSaved(
                        participant.warmup_choice_id, participant.warmup_submitted_at
                    )
                raise Conflict("WARMUP_ALREADY_SUBMITTED")
            if (
                target.status is not RunStatus.lobby
                or participant.status is not ParticipantStatus.waiting
            ):
                raise Conflict("RUN_STATE_CONFLICT")
            choices = {str(c["id"]) for c in (target.live_warmup or {}).get("choices", [])}
            if cmd.choice_id not in choices:
                raise InvalidInput(details={"choice_id": "unknown"})
            now = self._clock.now()
            if await self._uow.participants.submit_warmup(participant.id, cmd.choice_id, now):
                return WarmupSaved(cmd.choice_id, now)
            raise Conflict("WARMUP_ALREADY_SUBMITTED")
