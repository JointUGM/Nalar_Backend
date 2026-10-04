from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.administration import AdminRow
from nalar.application.ports.uow import UnitOfWork


class ExportScoresQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, publication_id: UUID) -> list[AdminRow]:
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            return await self._uow.results.publication_scores(publication_id)
