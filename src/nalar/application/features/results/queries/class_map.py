import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.results import ClassMapInput
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.class_map import ClassMap, aggregate
from nalar.domain.placeholders import NarrativeLexicon, PlaceholderError, counts_from_snapshot, fill

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilledInsight:
    narrative: str
    generated_at: datetime


class ClassMapQuery:
    def __init__(self, uow: UnitOfWork, lexicon: NarrativeLexicon) -> None:
        self._uow = uow
        self._lexicon = lexicon

    async def execute(
        self, actor_id: UUID, publication_id: UUID
    ) -> tuple[ClassMapInput, ClassMap, FilledInsight | None]:
        """The input carries the names and statements the counts refer to."""
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            data = await self._uow.results.class_map_input(publication_id)
            stored = await self._uow.release.latest_insight(publication_id)
        if data is None:
            raise NotFound()
        by_concept: defaultdict[UUID, list[UUID]] = defaultdict(list)
        for misconception_id, concept_id, _ in data.misconceptions:
            by_concept[concept_id].append(misconception_id)
        insight = None
        if stored is not None:
            try:
                narrative = fill(
                    stored.narrative, counts_from_snapshot(stored.counts_snapshot), self._lexicon
                )
                insight = FilledInsight(narrative, stored.generated_at)
            except PlaceholderError:
                log.warning(
                    "stored insight no longer fills", extra={"publication_id": str(publication_id)}
                )
        return data, aggregate([c for c, _ in data.concepts], by_concept, data.attempts), insight
