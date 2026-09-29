from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.results import Monitor
from nalar.application.ports.uow import UnitOfWork


class MonitorQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, publication_id: UUID) -> Monitor:
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            monitor = await self._uow.results.monitor(publication_id)
        if monitor is None:
            raise NotFound()
        return monitor
