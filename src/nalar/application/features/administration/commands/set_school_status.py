from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.uow import UnitOfWork


class SetSchoolStatusHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, school_id: UUID, active: bool) -> None:
        async with self._uow:
            if not await self._uow.authz.is_platform_admin(actor_id):
                raise NotFound()
            if not await self._uow.administration.lock_school(school_id):
                raise NotFound()
            if await self._uow.administration.set_school_status(school_id, active):
                await self._uow.audit.record(
                    school_id,
                    actor_id,
                    "platform.school_status",
                    "schools",
                    school_id,
                    {"status": "active" if active else "suspended"},
                )
