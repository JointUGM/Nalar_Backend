from uuid import UUID

from nalar.application.errors import InvalidInput
from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.features.administration.commands.request_digest import request_digest
from nalar.application.ports.administration import NewAcademicYear
from nalar.application.ports.uow import UnitOfWork


class CreateAcademicYearHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, school_id: UUID, details: NewAcademicYear, key: UUID
    ) -> UUID:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            if details.ends_on <= details.starts_on:
                raise InvalidInput()
            request_id, result = await self._uow.administration.request(
                actor_id, "academic_year", school_id, key, request_digest(details)
            )
            if result:
                return result
            result = await self._uow.administration.create_year(school_id, details)
            await self._uow.administration.finish_request(request_id, result)
            await self._uow.audit.record(
                school_id, actor_id, "admin.create_academic_year", "academic_years", result, {}
            )
            return result
