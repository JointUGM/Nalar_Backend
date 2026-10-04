from typing import Literal
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


class ArchiveHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def mission(self, actor_id: UUID, mission_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.authz.is_mission_creator(actor_id, mission_id):
                raise NotFound()
            school_id = await self._uow.missions.archive(mission_id, self._clock.now())
            await self._uow.audit.record(
                school_id, actor_id, "mission.archived", "missions", mission_id, {}
            )

    async def knowledge(
        self,
        actor_id: UUID,
        kb_id: UUID,
        kind: Literal["kb", "material", "concept"],
        item_id: UUID | None = None,
    ) -> None:
        async with self._uow:
            if not await self._uow.authz.owns_kb(actor_id, kb_id):
                raise NotFound()
            school_id = await self._uow.knowledge.archive(kb_id, kind, item_id, self._clock.now())
            await self._uow.audit.record(
                school_id,
                actor_id,
                f"{kind}.archived",
                {"kb": "knowledge_bases", "material": "teaching_materials", "concept": "concepts"}[
                    kind
                ],
                item_id or kb_id,
                {},
            )
