from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.missions import VersionHistoryEntry
from nalar.application.ports.uow import UnitOfWork


class ListVersionsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, mission_id: UUID) -> list[VersionHistoryEntry]:
        async with self._uow:
            creator = await self._uow.authz.is_mission_creator(actor_id, mission_id)
            mission = await self._uow.missions.mission_ref(mission_id)
            if mission is None or not (
                creator or await self._uow.authz.can_read_kb(actor_id, mission.knowledge_base_id)
            ):
                raise NotFound()
            return await self._uow.missions.version_history(mission_id, creator)
