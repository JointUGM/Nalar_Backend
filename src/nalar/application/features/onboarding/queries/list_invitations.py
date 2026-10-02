from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.activations import InvitationPage
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


class ListInvitationsQuery:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, actor_id: UUID, school_id: UUID, cursor: UUID | None, limit: int
    ) -> InvitationPage:
        async with self._uow:
            if not await self._uow.authz.is_school_admin(actor_id, school_id):
                raise NotFound()
            return await self._uow.activations.list_invitations(
                school_id, self._clock.now(), cursor, limit
            )
