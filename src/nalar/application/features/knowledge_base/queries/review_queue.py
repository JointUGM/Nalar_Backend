from uuid import UUID

from nalar.application.features.knowledge_base.commands.add_material import require_owner
from nalar.application.ports.uow import UnitOfWork


class ReviewQueueQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, kb_id: UUID) -> tuple[int, int]:
        async with self._uow:
            await require_owner(self._uow, actor_id, kb_id)
            return await self._uow.knowledge.review_queue(kb_id)
