from uuid import UUID

from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.uow import UnitOfWork


class ReactivatePersonHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, school_id: UUID, user_id: UUID) -> None:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            if await self._uow.administration.reactivate_person(school_id, user_id):
                await self._uow.audit.record(
                    school_id, actor_id, "admin.reactivate_person", "profiles", user_id, {}
                )
