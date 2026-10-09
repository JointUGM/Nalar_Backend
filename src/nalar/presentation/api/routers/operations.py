from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Response
from pydantic import AwareDatetime

from nalar.application.features.administration.queries.platform_operations import (
    PlatformOperationsHandler,
)
from nalar.application.features.knowledge_base.commands.archive import ArchiveHandler
from nalar.application.features.knowledge_base.queries.material_file import (
    MaterialFilePolicy,
    MaterialFileQuery,
)
from nalar.application.features.notifications.inbox import InboxHandler
from nalar.application.features.parents.commands.mark_seen import MarkSeenHandler
from nalar.application.features.results.queries.export_scores import ExportScoresQuery
from nalar.application.features.results.queries.session_report import SessionReportQuery
from nalar.application.features.results.queries.student_history import StudentHistoryQuery
from nalar.application.features.runs.commands.remove_participant import RemoveParticipantHandler
from nalar.application.features.sessions.commands.end_session import EndSessionHandler
from nalar.application.features.telemetry.client_events import (
    ClientEventsHandler,
)
from nalar.application.ports.client_events import ClientEvent
from nalar.presentation.api.csv_export import csv_response
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.administration import SchoolOut
from nalar.presentation.api.schemas.operations import (
    AiUsageOut,
    AuditPageOut,
    ClientEventsIn,
    ClientEventsOut,
    MaterialFileOut,
    NotificationsOut,
    SchoolPatchIn,
    StudentHistoryOut,
)

router = APIRouter(route_class=DishkaRoute)
Limit = Annotated[int, Query(ge=1, le=100)]


@router.get("/notifications", response_model=NotificationsOut, tags=["notifications"])
async def notifications(
    user: CurrentUser,
    handler: FromDishka[InboxHandler],
    limit: Limit = 20,
    cursor: str | None = Query(None, max_length=512),
) -> NotificationsOut:
    return NotificationsOut.model_validate(await handler.page(user.id, limit, cursor))


@router.post("/notifications/{notification_id}/read", status_code=204, tags=["notifications"])
async def read_notification(
    notification_id: UUID, user: CurrentUser, handler: FromDishka[InboxHandler]
) -> Response:
    await handler.read(user.id, notification_id)
    return Response(status_code=204)


@router.post("/client-events", status_code=202, response_model=ClientEventsOut, tags=["telemetry"])
async def client_events(
    body: ClientEventsIn,
    user: CurrentUser,
    handler: FromDishka[ClientEventsHandler],
) -> ClientEventsOut:
    count = await handler.execute(
        user.id,
        [ClientEvent(**item.model_dump()) for item in body.events],
    )
    return ClientEventsOut(accepted=count)


@router.post("/missions/{mission_id}/archive", status_code=204, tags=["missions"])
async def archive_mission(
    mission_id: UUID, user: CurrentUser, handler: FromDishka[ArchiveHandler]
) -> Response:
    await handler.mission(user.id, mission_id)
    return Response(status_code=204)


@router.delete("/knowledge-bases/{kb_id}", status_code=204, tags=["knowledge-base"])
@router.post("/knowledge-bases/{kb_id}/archive", status_code=204, tags=["knowledge-base"])
async def archive_kb(
    kb_id: UUID, user: CurrentUser, handler: FromDishka[ArchiveHandler]
) -> Response:
    await handler.knowledge(user.id, kb_id, "kb")
    return Response(status_code=204)


@router.delete(
    "/knowledge-bases/{kb_id}/materials/{material_id}", status_code=204, tags=["knowledge-base"]
)
async def delete_material(
    kb_id: UUID, material_id: UUID, user: CurrentUser, handler: FromDishka[ArchiveHandler]
) -> Response:
    await handler.knowledge(user.id, kb_id, "material", material_id)
    return Response(status_code=204)


@router.post(
    "/knowledge-bases/{kb_id}/concepts/{concept_id}/archive",
    status_code=204,
    tags=["knowledge-base"],
)
async def archive_concept(
    kb_id: UUID, concept_id: UUID, user: CurrentUser, handler: FromDishka[ArchiveHandler]
) -> Response:
    await handler.knowledge(user.id, kb_id, "concept", concept_id)
    return Response(status_code=204)


@router.get(
    "/knowledge-bases/{kb_id}/materials/{material_id}/file",
    response_model=MaterialFileOut,
    tags=["knowledge-base"],
)
async def material_file(
    kb_id: UUID,
    material_id: UUID,
    user: CurrentUser,
    response: Response,
    query: FromDishka[MaterialFileQuery],
    settings: FromDishka[MaterialFilePolicy],
) -> MaterialFileOut:
    response.headers["Cache-Control"] = "private, no-store"
    url = await query.execute(user.id, kb_id, material_id, settings.material_url_expiry_s)
    return MaterialFileOut(url=url, expires_in=settings.material_url_expiry_s)


@router.post("/sessions/{session_id}/end", status_code=204, tags=["teacher"])
async def end_session(
    session_id: UUID, user: CurrentUser, handler: FromDishka[EndSessionHandler]
) -> Response:
    await handler.execute(user.id, session_id)
    return Response(status_code=204)


@router.post(
    "/runs/{run_id}/participants/{participant_id}/remove", status_code=204, tags=["teacher"]
)
async def remove_participant(
    run_id: UUID,
    participant_id: UUID,
    user: CurrentUser,
    handler: FromDishka[RemoveParticipantHandler],
) -> Response:
    await handler.remove(user.id, run_id, participant_id)
    return Response(status_code=204)


@router.post("/student/runs/{run_id}/leave", status_code=204, tags=["student"])
async def leave_lobby(
    run_id: UUID, user: CurrentUser, handler: FromDishka[RemoveParticipantHandler]
) -> Response:
    await handler.leave(user.id, run_id)
    return Response(status_code=204)


@router.post("/parent/children/{student_id}/seen", status_code=204, tags=["parent"])
async def mark_seen(
    student_id: UUID, user: CurrentUser, handler: FromDishka[MarkSeenHandler]
) -> Response:
    await handler.execute(user.id, student_id)
    return Response(status_code=204)


@router.get(
    "/teacher/students/{student_id}/history", response_model=StudentHistoryOut, tags=["teacher"]
)
async def student_history(
    student_id: UUID,
    user: CurrentUser,
    query: FromDishka[StudentHistoryQuery],
    limit: Limit = 20,
    cursor: str | None = Query(None, max_length=512),
) -> StudentHistoryOut:
    return StudentHistoryOut.model_validate(await query.execute(user.id, student_id, limit, cursor))


@router.get("/platform/schools/{school_id}", response_model=SchoolOut, tags=["platform"])
async def school_detail(
    school_id: UUID, user: CurrentUser, handler: FromDishka[PlatformOperationsHandler]
) -> SchoolOut:
    return SchoolOut.model_validate(await handler.school(user.id, school_id))


@router.patch("/platform/schools/{school_id}", response_model=SchoolOut, tags=["platform"])
async def edit_school(
    school_id: UUID,
    body: SchoolPatchIn,
    user: CurrentUser,
    handler: FromDishka[PlatformOperationsHandler],
) -> SchoolOut:
    return SchoolOut.model_validate(
        await handler.school(user.id, school_id, body.model_dump(exclude_unset=True))
    )


@router.get("/platform/ai-usage", response_model=list[AiUsageOut], tags=["platform"])
async def ai_usage(
    user: CurrentUser,
    handler: FromDishka[PlatformOperationsHandler],
    start: Annotated[AwareDatetime, Query(alias="from")],
    end: Annotated[AwareDatetime, Query(alias="to")],
) -> list[AiUsageOut]:
    return [AiUsageOut.model_validate(row) for row in await handler.usage(user.id, start, end)]


@router.get("/platform/audit-log", response_model=AuditPageOut, tags=["platform"])
async def platform_audit(
    user: CurrentUser,
    handler: FromDishka[PlatformOperationsHandler],
    limit: Limit = 20,
    cursor: int | None = Query(None, ge=1),
) -> AuditPageOut:
    return AuditPageOut.model_validate(await handler.audit(user.id, None, limit, cursor))


@router.get("/schools/{school_id}/audit-log", response_model=AuditPageOut, tags=["administration"])
async def school_audit(
    school_id: UUID,
    user: CurrentUser,
    handler: FromDishka[PlatformOperationsHandler],
    limit: Limit = 20,
    cursor: int | None = Query(None, ge=1),
) -> AuditPageOut:
    return AuditPageOut.model_validate(await handler.audit(user.id, school_id, limit, cursor))


@router.get(
    "/publications/{publication_id}/export",
    tags=["teacher"],
    responses={200: {"content": {"text/csv": {}}}},
    response_class=Response,
)
async def export_publication(
    publication_id: UUID,
    user: CurrentUser,
    query: FromDishka[ExportScoresQuery],
    format: Literal["csv"] = "csv",
) -> Response:
    rows = await query.execute(user.id, publication_id)
    return csv_response(
        f"publication-{publication_id}.csv",
        [
            "session_id",
            "student_id",
            "student_name",
            "mission_title",
            "mission_version",
            "class_name",
            "attempt_number",
            "status",
            "evaluation_status",
            "dimension",
            "final_level",
        ],
        rows,
    )


@router.get(
    "/sessions/{session_id}/report/export",
    tags=["teacher"],
    responses={200: {"content": {"text/csv": {}}}},
    response_class=Response,
)
async def export_report(
    session_id: UUID,
    user: CurrentUser,
    query: FromDishka[SessionReportQuery],
    format: Literal["csv"] = "csv",
) -> Response:
    report = await query.execute(user.id, session_id)
    rows = [
        {
            "student_name": report.student_name,
            "mission_title": report.mission.title,
            "mission_version": report.mission.version_number,
            "status": report.status,
            "attempt_number": report.attempt_number,
            **asdict(score),
        }
        for score in report.scores
    ]
    return csv_response(
        f"session-{session_id}.csv",
        [
            "student_name",
            "mission_title",
            "mission_version",
            "status",
            "attempt_number",
            "dimension",
            "ai_level",
            "final_level",
            "rationale",
        ],
        rows,
    )
