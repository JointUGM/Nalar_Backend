from datetime import datetime
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


class EditPublicationHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self,
        actor_id: UUID,
        publication_id: UUID,
        opens_at: datetime | None = None,
        closes_at: datetime | None = None,
        *,
        cancel: bool = False,
    ) -> None:
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            school_id = await self._uow.publications.edit(
                publication_id, self._clock.now(), opens_at, closes_at, cancel=cancel
            )
            await self._uow.audit.record(
                school_id,
                actor_id,
                "publication.cancel" if cancel else "publication.edit",
                "publications",
                publication_id,
                {} if cancel else {"opens_at": str(opens_at), "closes_at": str(closes_at)},
            )
