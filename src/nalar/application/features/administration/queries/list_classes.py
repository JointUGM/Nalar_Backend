from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.administration import AdminRow
from nalar.application.ports.uow import UnitOfWork


class ListClassesQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, school_id: UUID, academic_year_id: UUID | None
    ) -> list[AdminRow]:
        async with self._uow:
            if not await self._uow.authz.is_school_admin(actor_id, school_id):
                raise NotFound()
            return await self._uow.administration.classes(school_id, academic_year_id)
