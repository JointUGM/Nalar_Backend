from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class CreateMission:
    actor_id: UUID
    kb_id: UUID
    title: str
    learning_objective: str


class CreateMissionHandler:
    """D-S26-3: creates the mission row only; versions are authored by hand until S2."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, cmd: CreateMission) -> UUID:
        async with self._uow:
            if not await self._uow.authz.can_read_kb(cmd.actor_id, cmd.kb_id):
                raise NotFound()
            school_id = await self._uow.missions.kb_school_id(cmd.kb_id)
            assert school_id is not None
            return await self._uow.missions.create_mission(
                school_id,
                cmd.kb_id,
                cmd.actor_id,
                cmd.title.strip(),
                cmd.learning_objective.strip(),
            )
