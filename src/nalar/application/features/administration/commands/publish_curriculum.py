from uuid import UUID

from nalar.application.errors import InvalidInput, NotFound
from nalar.application.features.administration.commands.request_digest import request_digest
from nalar.application.ports.administration import NewCurriculum
from nalar.application.ports.uow import UnitOfWork


class PublishCurriculumHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, details: NewCurriculum, key: UUID) -> UUID:
        async with self._uow:
            if not await self._uow.authz.is_platform_admin(actor_id):
                raise NotFound()
            if not details.subjects or len({(s.name, s.phase) for s in details.subjects}) != len(
                details.subjects
            ):
                raise InvalidInput()
            request_id, result = await self._uow.administration.request(
                actor_id, "curriculum", actor_id, key, request_digest(details)
            )
            if result:
                return result
            result = await self._uow.administration.publish_curriculum(details)
            await self._uow.administration.finish_request(request_id, result)
            await self._uow.audit.record(
                None, actor_id, "platform.publish_curriculum", "cp_versions", result, {}
            )
            return result
