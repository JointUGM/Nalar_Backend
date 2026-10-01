from uuid import UUID

from nalar.application.errors import DependencyUnavailable, NotFound
from nalar.application.ports.uow import UnitOfWork


class GenerateMissionHandler:
    # TODO(S2): call /v1/s2 with get_kb_chunks grounding once the AI service ships it.
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, mission_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.authz.is_mission_creator(actor_id, mission_id):
                raise NotFound()
        raise DependencyUnavailable(
            "MISSION_GENERATION_UNAVAILABLE",
            "Pembuatan misi otomatis belum tersedia. Susun versi misi secara manual.",
        )
