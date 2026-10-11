from uuid import UUID

from nalar.application.errors import Forbidden, NotFound
from nalar.application.ports.uow import UnitOfWork


class ReviewQueueQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, kb_id: UUID) -> tuple[int, int]:
        async with self._uow:
            if not await self._uow.authz.owns_kb(actor_id, kb_id):
                if await self._uow.authz.can_read_kb(actor_id, kb_id):
                    raise Forbidden("NOT_OWNER")
                raise NotFound()
            return await self._uow.knowledge.review_queue(kb_id)
