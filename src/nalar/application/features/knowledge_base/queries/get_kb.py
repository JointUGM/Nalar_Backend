from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.knowledge import KbDetail
from nalar.application.ports.uow import UnitOfWork


class GetKbQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, kb_id: UUID) -> tuple[KbDetail, bool]:
        """The detail and can_edit. Non-owners see approved items only (contract)."""
        async with self._uow:
            if not await self._uow.authz.can_read_kb(actor_id, kb_id):
                raise NotFound()
            owner = await self._uow.authz.owns_kb(actor_id, kb_id)
            detail = await self._uow.knowledge.kb_detail(kb_id, approved_only=not owner)
        if detail is None:
            raise NotFound()
        return detail, owner
