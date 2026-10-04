import csv
import io
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, File, Form, Query, Response, UploadFile

from nalar.application.features.roster.commands.upload_roster import (
    RosterLimits,
    UploadRoster,
    UploadRosterHandler,
)
from nalar.application.features.roster.queries.get_import import GetImportQuery
from nalar.application.features.roster.queries.list_academic_years import ListAcademicYearsQuery
from nalar.application.features.roster.queries.list_imports import ListImportsQuery
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.admin import (
    AcademicYearOut,
    RosterImportOut,
    RosterImportsPageOut,
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


@router.get("/schools/{school_id}/roster-imports", response_model=RosterImportsPageOut)
async def list_imports(
    school_id: UUID,
    user: CurrentUser,
    query: FromDishka[ListImportsQuery],
    academic_year_id: UUID | None = None,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RosterImportsPageOut:
    return RosterImportsPageOut.model_validate(
        asdict(await query.execute(user.id, school_id, academic_year_id, cursor, limit))
    )


@router.get("/roster-imports/{import_id}/errors.csv", response_class=Response)
async def import_errors(
    import_id: UUID,
    user: CurrentUser,
    query: FromDishka[GetImportQuery],
) -> Response:
    view = await query.execute(user.id, import_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["row_number", "field", "message"])
    for error in view.errors:
        values = [error.field, error.message]
        writer.writerow(
            [
                error.row_number,
                *[
                    "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
                    for value in values
                ],
            ]
        )
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="roster-{import_id}-errors.csv"',
            "Cache-Control": "no-store",
        },
    )
