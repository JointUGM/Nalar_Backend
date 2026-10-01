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
            if job.kind == ROSTER_KIND and not await self._uow.authz.can_manage_roster(
                actor_id, job.entity_id
            ):
                raise NotFound()
        return job
