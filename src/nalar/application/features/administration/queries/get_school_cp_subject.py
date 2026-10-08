from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.administration import AdminRow
from nalar.application.ports.uow import UnitOfWork


class GetSchoolCpSubjectQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, school_id: UUID, version_id: UUID, cp_subject_id: UUID
    ) -> AdminRow:
        async with self._uow:
            if not await self._uow.authz.is_school_admin(actor_id, school_id):
                raise NotFound()
            subject = await self._uow.administration.published_cp_subject(version_id, cp_subject_id)
            if subject is None:
                raise NotFound()
            return subject
