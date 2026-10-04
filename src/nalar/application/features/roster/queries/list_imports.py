from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.administration import AdminPage
from nalar.application.ports.uow import UnitOfWork


class ListImportsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        actor_id: UUID,
        school_id: UUID,
        year_id: UUID | None,
        cursor: UUID | None,
        limit: int,
    ) -> AdminPage:
        async with self._uow:
            if not await self._uow.authz.is_school_admin(actor_id, school_id):
                raise NotFound()
            return await self._uow.administration.roster_imports(school_id, year_id, cursor, limit)
