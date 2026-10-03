from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.administration import AdminRow
from nalar.application.ports.uow import UnitOfWork


class GetCurriculumVersionQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, version_id: UUID) -> AdminRow:
        async with self._uow:
            if not await self._uow.authz.is_platform_admin(actor_id):
                raise NotFound()
            result = await self._uow.administration.curriculum_version(version_id)
            if result is None:
                raise NotFound()
            return result
