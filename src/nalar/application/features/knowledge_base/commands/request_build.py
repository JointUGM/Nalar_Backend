from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.features.knowledge_base.commands.add_material import require_owner
from nalar.application.features.knowledge_base.commands.build_section import S1Settings
from nalar.application.features.knowledge_base.messages import build_message
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import KB_QUEUE
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class RequestBuild:
    actor_id: UUID
    kb_id: UUID
    section_id: UUID


@dataclass(frozen=True)
class BuildQueued:
    job_id: UUID
    section_id: UUID


class RequestBuildHandler:
    """Returns a fresh existing job or queues an owner-requested section build."""

    def __init__(self, uow: UnitOfWork, clock: Clock, settings: S1Settings) -> None:
        self._uow = uow
        self._clock = clock
        self._stale_after = timedelta(seconds=settings.stale_after_s)

    async def execute(self, cmd: RequestBuild) -> BuildQueued:
        async with self._uow:
            await require_owner(self._uow, cmd.actor_id, cmd.kb_id)
            section = await self._uow.knowledge.section_for_build(cmd.section_id)
            if section is None or section.knowledge_base_id != cmd.kb_id:
                raise NotFound()
            fresh = (
                section.job_updated_at is not None
                and section.job_updated_at > self._clock.now() - self._stale_after
            )
            if section.build_status in ("queued", "building") and fresh:
                assert section.job_id is not None
                return BuildQueued(section.job_id, section.id)
            if await self._uow.knowledge.overlaps(section.id):
                raise Conflict("SECTION_OVERLAP")
            await self._uow.knowledge.queue_section(section.id)
            job_id = await self._uow.jobs.create(
                kind="kb_build_section",
                entity_type="material_sections",
                entity_id=section.id,
                school_id=section.school_id,
                requested_by=cmd.actor_id,
            )
            await self._uow.queue.send(KB_QUEUE, build_message(section.id, job_id))
        return BuildQueued(job_id, section.id)
