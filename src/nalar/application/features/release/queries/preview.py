from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.release import SummaryPreview
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.release import readiness


@dataclass(frozen=True)
class ReleasePreview:
    ready: bool
    blockers: tuple[tuple[str, int], ...]
    eligible_count: int
    ineligible_count: int
    summaries: list[SummaryPreview]
    released_at: datetime | None


class ReleasePreviewQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, publication_id: UUID) -> ReleasePreview:
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            text = await self._uow.release.publication_text(publication_id)
            counts = await self._uow.release.counts(publication_id)
            summaries = await self._uow.release.preview_summaries(publication_id)
        assert text is not None
        state = readiness(counts)
        return ReleasePreview(
            state.ready and text.released_at is None,
            state.blockers,
            counts.eligible,
            counts.ineligible,
            summaries,
            text.released_at,
        )
