from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.knowledge import SectionView
from nalar.application.ports.uow import UnitOfWork


class ListSectionsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, kb_id: UUID) -> list[SectionView]:
        async with self._uow:
            if not await self._uow.authz.can_read_kb(actor_id, kb_id):
                raise NotFound()
            return await self._uow.knowledge.sections(kb_id)
