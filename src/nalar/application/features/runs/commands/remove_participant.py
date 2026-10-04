from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import RunStatus


class RemoveParticipantHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def remove(self, actor_id: UUID, run_id: UUID, participant_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.authz.teaches_run(actor_id, run_id):
                raise NotFound()
            run = await self._uow.runs.lock(run_id)
            if run is None:
                raise NotFound()
            if run.status is not RunStatus.lobby:
                raise Conflict("RUN_NOT_IN_LOBBY")
            await self._uow.participants.cancel_waiting(run_id, participant_id, None)
            await self._uow.audit.record(
                run.school_id,
                actor_id,
                "participant.removed",
                "run_participants",
                participant_id,
                {},
            )

    async def leave(self, actor_id: UUID, run_id: UUID) -> None:
        async with self._uow:
            target = await self._uow.participants.run_target(run_id)
            if target is None or not await self._uow.authz.is_enrolled(actor_id, target.class_id):
                raise NotFound()
            run = await self._uow.runs.lock(run_id)
            if run is None or run.status is not RunStatus.lobby:
                raise Conflict("RUN_NOT_IN_LOBBY")
            await self._uow.participants.cancel_waiting(run_id, None, actor_id)
