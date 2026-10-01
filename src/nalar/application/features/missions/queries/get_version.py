from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.missions import VersionRecord
from nalar.application.ports.uow import UnitOfWork


class GetVersionQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, mission_id: UUID, number: int
    ) -> tuple[VersionRecord, bool]:
        """The version and can_edit. Others' drafts are invisible (contract: creator only)."""
        async with self._uow:
            mission = await self._uow.missions.mission_ref(mission_id)
            if mission is None:
                raise NotFound()
            creator = await self._uow.authz.is_mission_creator(actor_id, mission_id)
            reader = creator or await self._uow.authz.can_read_kb(
                actor_id, mission.knowledge_base_id
            )
            version = await self._uow.missions.version(mission_id, number) if reader else None
        if version is None or (not creator and version.status == "draft"):
            raise NotFound()
        return version, creator and version.status == "draft"
