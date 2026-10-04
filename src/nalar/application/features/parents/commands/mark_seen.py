from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


class MarkSeenHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, actor_id: UUID, student_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.authz.is_linked_parent(actor_id, student_id):
                raise NotFound()
            await self._uow.parents.mark_seen(actor_id, student_id, self._clock.now())
