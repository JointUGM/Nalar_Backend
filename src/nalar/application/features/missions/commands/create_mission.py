import hashlib
import json
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
    request_key: UUID | None = None


class CreateMissionHandler:
    """D-S26-3: creates the mission row only; versions are authored by hand until S2."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, cmd: CreateMission) -> UUID:
        async with self._uow:
            if not await self._uow.authz.can_read_kb(cmd.actor_id, cmd.kb_id):
                raise NotFound()
            await self._uow.knowledge.require_active(cmd.kb_id)
            school_id = await self._uow.missions.kb_school_id(cmd.kb_id)
            assert school_id is not None
            request_id = None
            if cmd.request_key:
                digest = hashlib.sha256(
                    json.dumps(
                        [str(cmd.kb_id), cmd.title.strip(), cmd.learning_objective.strip()]
                    ).encode()
                ).hexdigest()
                request_id, result_id = await self._uow.administration.request(
                    cmd.actor_id, "mission.create", cmd.kb_id, cmd.request_key, digest
                )
                if result_id:
                    return result_id
            result_id = await self._uow.missions.create_mission(
                school_id,
                cmd.kb_id,
                cmd.actor_id,
                cmd.title.strip(),
                cmd.learning_objective.strip(),
            )
            if request_id:
                await self._uow.administration.finish_request(request_id, result_id)
            return result_id
