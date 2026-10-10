from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from nalar.application.features.jobs.queries.get_job import GetJobQuery
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.jobs import JobOut, MissionGenerationResultOut

router = APIRouter(tags=["jobs"], route_class=DishkaRoute)


@router.get("/jobs/{job_id}", response_model=JobOut, response_model_exclude_unset=True)
async def get_job(job_id: UUID, user: CurrentUser, query: FromDishka[GetJobQuery]) -> JobOut:
    job = await query.execute(user.id, job_id)
    result = JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        entity_type=job.entity_type,
        entity_id=job.entity_id,
        error_code=job.error_code,
        updated_at=job.updated_at,
    )
    if (
        job.kind in ("mission_generate", "mission_revise")
        and job.status == "succeeded"
        and job.result
    ):
        result.generation_result = MissionGenerationResultOut.model_validate(job.result)
    return result
