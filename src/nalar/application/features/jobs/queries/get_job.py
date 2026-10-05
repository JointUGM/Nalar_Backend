from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.features.roster.messages import ROSTER_KIND
from nalar.application.ports.jobs import Job
from nalar.application.ports.uow import UnitOfWork


class GetJobQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, job_id: UUID) -> Job:
        async with self._uow:
            job = await self._uow.jobs.get(job_id)
            if job is None or job.requested_by != actor_id:
                raise NotFound()
            if job.kind == ROSTER_KIND:
                if not await self._uow.authz.can_manage_roster(actor_id, job.entity_id):
                    raise NotFound()
            elif job.kind in ("national_reference_extract", "national_reference_index"):
                if not await self._uow.authz.is_platform_admin(actor_id):
                    raise NotFound()
            elif job.kind == "mission_generate":
                if not await self._uow.authz.is_mission_creator(actor_id, job.entity_id):
                    raise NotFound()
            elif job.school_id is None or not await self._uow.authz.is_school_teacher(
                actor_id, job.school_id
            ):
                raise NotFound()
        return job
