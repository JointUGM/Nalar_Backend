from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.results import TrailInput
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.misconception_trail import Trail, trail


class MisconceptionTrailQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, publication_id: UUID, earlier_publication_id: UUID | None
    ) -> tuple[TrailInput, Trail | None]:
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            data = await self._uow.results.misconception_trail_input(
                publication_id, earlier_publication_id
            )
        if data is None:
            raise NotFound()
        if data.earlier is None:
            return data, None
        found = trail(
            [(m, c) for m, c, _ in data.earlier.misconceptions],
            {c for c, _ in data.concepts},
            {m for m, _, _ in data.to.misconceptions},
            data.earlier.attempts,
            data.to.attempts,
        )
        return data, found
