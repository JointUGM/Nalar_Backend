from collections import defaultdict
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.results import ClassMapInput
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.class_map import ClassMap, aggregate


class ClassMapQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, publication_id: UUID) -> tuple[ClassMapInput, ClassMap]:
        """The input carries the names and statements the counts refer to."""
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            data = await self._uow.results.class_map_input(publication_id)
        if data is None:
            raise NotFound()
        by_concept: defaultdict[UUID, list[UUID]] = defaultdict(list)
        for misconception_id, concept_id, _ in data.misconceptions:
            by_concept[concept_id].append(misconception_id)
        return data, aggregate([c for c, _ in data.concepts], by_concept, data.attempts)
