from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from nalar.application.features.jobs.queries.get_job import GetJobQuery
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.jobs import JobOut

router = APIRouter(tags=["jobs"], route_class=DishkaRoute)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: UUID, user: CurrentUser, query: FromDishka[GetJobQuery]) -> JobOut:
    job = await query.execute(user.id, job_id)
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        entity_type=job.entity_type,
        entity_id=job.entity_id,
        error_code=job.error_code,
        updated_at=job.updated_at,
    )
