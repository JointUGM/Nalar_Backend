from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.features.jobs.queries.get_job import GetJobQuery
from nalar.application.ports.missions import RevisionRequestRecord
from nalar.application.ports.uow import UnitOfWork


class GetRevisionRequestQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, mission_id: UUID, job_id: UUID
    ) -> tuple[RevisionRequestRecord, str]:
        job = await GetJobQuery(self._uow).execute(actor_id, job_id)
        if (
            job.kind != "mission_revise"
            or job.entity_type != "missions"
            or job.entity_id != mission_id
        ):
            raise NotFound()
        async with self._uow:
            request = await self._uow.missions.revision_request(job_id)
        if request is None:
            raise NotFound()
        return request, job.status
