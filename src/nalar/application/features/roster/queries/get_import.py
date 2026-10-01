from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.roster import ImportView
from nalar.application.ports.uow import UnitOfWork


class GetImportQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, import_id: UUID) -> ImportView:
        async with self._uow:
            if not await self._uow.authz.can_manage_roster(actor_id, import_id):
                raise NotFound()
            view = await self._uow.roster.import_view(import_id)
        assert view is not None
        return view
