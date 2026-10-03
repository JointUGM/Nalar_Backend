from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.administration import AdminPage
from nalar.application.ports.uow import UnitOfWork


class ListSchoolsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, q: str, cursor: UUID | None, limit: int) -> AdminPage:
        async with self._uow:
            if not await self._uow.authz.is_platform_admin(actor_id):
                raise NotFound()
            return await self._uow.administration.schools(q, cursor, limit)
