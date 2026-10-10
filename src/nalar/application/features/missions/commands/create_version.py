from dataclasses import dataclass, replace
from uuid import UUID

from nalar.application.errors import Forbidden, NotFound, Unprocessable
from nalar.application.ports.missions import MissionRef, VersionDraft
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.context_pack import derive_probe_plan, question_bank_from_pack


@dataclass(frozen=True)
class CreateVersion:
    actor_id: UUID
    mission_id: UUID
    draft: VersionDraft


@dataclass(frozen=True)
class VersionCreated:
    version_id: UUID
    version_number: int


async def require_creator(uow: UnitOfWork, actor_id: UUID, mission_id: UUID) -> MissionRef:
    mission = await uow.missions.mission_ref(mission_id)
    if mission is None:
        raise NotFound()
    if mission.created_by == actor_id and await uow.authz.is_mission_creator(actor_id, mission_id):
        return mission
    if await uow.authz.can_read_kb(actor_id, mission.knowledge_base_id):
        raise Forbidden("NOT_OWNER")
    raise NotFound()


def invalid(problems: list[tuple[str, str]]) -> Unprocessable:
    return Unprocessable(
        "MISSION_VERSION_INVALID",
        "Versi misi belum valid.",
        {"problems": [{"code": c, "detail": d} for c, d in problems]},
    )


class CreateVersionHandler:
    """TC-5: always a new version; probe_plan is the bank grouped by move."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, cmd: CreateVersion) -> VersionCreated:
        d = cmd.draft
        async with self._uow:
            mission = await require_creator(self._uow, cmd.actor_id, cmd.mission_id)
            if d.base_version_id is not None:
                base = await self._uow.missions.version_by_id(mission.id, d.base_version_id)
                if base is None:
                    raise NotFound()
                d = replace(
                    d, learning_objective=base.draft.learning_objective, title=base.draft.title
                )
            problems = await self._uow.missions.item_problems(
                mission.knowledge_base_id,
                mission.school_id,
                d.target_concept_ids,
                d.misconception_ids,
                d.source_chunk_ids,
            )
            if problems:
                raise invalid(problems)
            plan = derive_probe_plan(question_bank_from_pack({"question_bank": d.question_bank}))
            number = await self._uow.missions.next_version_number(mission.id)
            version_id = await self._uow.missions.insert_version(
                mission, number, cmd.actor_id, d, plan
            )
        return VersionCreated(version_id, number)
