from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.roster import AcademicYear
from nalar.application.ports.uow import UnitOfWork


class ListAcademicYearsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, school_id: UUID) -> list[AcademicYear]:
        async with self._uow:
            if not await self._uow.authz.is_school_admin(actor_id, school_id):
                raise NotFound()
            return await self._uow.roster.academic_years(school_id)
