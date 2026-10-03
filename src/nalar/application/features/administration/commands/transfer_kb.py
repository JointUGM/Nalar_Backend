from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.uow import UnitOfWork


class TransferKbHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, kb_id: UUID, teacher_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.authz.manages_kb(actor_id, kb_id):
                raise NotFound()
            school_id = await self._uow.administration.kb_school(kb_id)
            if school_id is None:
                raise NotFound()
            await authorize_school_write(self._uow, actor_id, school_id)
            await self._uow.administration.transfer_kb(school_id, kb_id, teacher_id)
            await self._uow.audit.record(
                school_id,
                actor_id,
                "admin.transfer_kb",
                "knowledge_bases",
                kb_id,
                {"teacher_id": str(teacher_id)},
            )
