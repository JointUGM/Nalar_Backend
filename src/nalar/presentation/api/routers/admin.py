from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, File, Form, UploadFile

from nalar.application.features.roster.commands.upload_roster import (
    RosterLimits,
    UploadRoster,
    UploadRosterHandler,
)
from nalar.application.features.roster.queries.get_import import GetImportQuery
from nalar.application.features.roster.queries.list_academic_years import ListAcademicYearsQuery
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.admin import (
    AcademicYearOut,
    RosterImportOut,
    RosterQueuedOut,
    RowErrorOut,
)

router = APIRouter(tags=["admin"], route_class=DishkaRoute)


@router.get("/schools/{school_id}/academic-years", response_model=list[AcademicYearOut])
async def academic_years(
    school_id: UUID, user: CurrentUser, query: FromDishka[ListAcademicYearsQuery]
) -> list[AcademicYearOut]:
    return [AcademicYearOut(**asdict(year)) for year in await query.execute(user.id, school_id)]


@router.post("/schools/{school_id}/roster-imports", status_code=202, response_model=RosterQueuedOut)
async def upload_roster(
    school_id: UUID,
    user: CurrentUser,
    academic_year_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
    handler: FromDishka[UploadRosterHandler],
    limits: FromDishka[RosterLimits],
) -> RosterQueuedOut:
    data = await file.read(limits.max_bytes + 1)
    done = await handler.execute(UploadRoster(user.id, school_id, academic_year_id, data))
    return RosterQueuedOut(import_id=done.import_id, job_id=done.job_id)


@router.get("/roster-imports/{import_id}", response_model=RosterImportOut)
async def get_import(
    import_id: UUID, user: CurrentUser, query: FromDishka[GetImportQuery]
) -> RosterImportOut:
    view = await query.execute(user.id, import_id)
    return RosterImportOut(
        status=view.status,
        rows_total=view.rows_total,
        rows_succeeded=view.rows_succeeded,
        rows_failed=view.rows_failed,
        errors=[RowErrorOut(**asdict(e)) for e in view.errors],
    )
