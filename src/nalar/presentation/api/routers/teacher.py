from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Header, Query

from nalar.application.features.publications.commands.grant_attempt import (
    GrantAttempt,
    GrantAttemptHandler,
)
from nalar.application.features.publications.commands.publish import Publish, PublishHandler
from nalar.application.features.publications.queries.teacher_assignments import (
    TeacherAssignmentsQuery,
)
from nalar.application.features.publications.queries.teacher_publications import (
    TeacherPublications,
    TeacherPublicationsQuery,
)
from nalar.application.features.results.queries.class_students import (
    ClassStudents,
    ClassStudentsQuery,
)
from nalar.application.features.results.queries.teacher_attention import (
    TeacherAttention,
    TeacherAttentionQuery,
)
from nalar.application.features.results.queries.teacher_dashboard import TeacherDashboardQuery
from nalar.application.ports.publications import NewRun
from nalar.domain.labels import RunMode
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.teacher import (
    AssignmentOut,
    AssignmentsOut,
    AttemptGrantIn,
    AttemptGrantOut,
    AttentionPageOut,
    ClassStudentsOut,
    CountsOut,
    PublicationOut,
    PublicationsPageOut,
    PublishIn,
    PublishOut,
    RunSummaryOut,
    TeacherDashboardOut,
)

router = APIRouter(tags=["teacher"], route_class=DishkaRoute)


@router.post(
    "/publications/{publication_id}/attempt-grants", status_code=201, response_model=AttemptGrantOut
)
async def grant_attempt(
    publication_id: UUID,
    body: AttemptGrantIn,
    user: CurrentUser,
    handler: FromDishka[GrantAttemptHandler],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> AttemptGrantOut:
    return AttemptGrantOut(
        **asdict(
            await handler.execute(
                GrantAttempt(
                    user.id,
                    publication_id,
                    body.student_id,
                    body.reason,
                    idempotency_key,
                    body.opens_at,
                    body.closes_at,
                )
            )
        )
    )


@router.get("/teacher/dashboard", response_model=TeacherDashboardOut)
async def dashboard(
    user: CurrentUser, query: FromDishka[TeacherDashboardQuery], school_id: UUID
) -> TeacherDashboardOut:
    return TeacherDashboardOut.model_validate(asdict(await query.execute(user.id, school_id)))


@router.get("/teacher/classes/{class_id}/students", response_model=ClassStudentsOut)
async def class_students(
    class_id: UUID,
    user: CurrentUser,
    query: FromDishka[ClassStudentsQuery],
    publication_id: UUID | None = None,
) -> ClassStudentsOut:
    rows = await query.execute(ClassStudents(user.id, class_id, publication_id))
    return ClassStudentsOut.model_validate(
        {
            "items": [asdict(row) for row in rows],
            "publication_id": publication_id,
        }
    )


@router.get("/teacher/attention", response_model=AttentionPageOut)
async def attention(
    user: CurrentUser,
    query: FromDishka[TeacherAttentionQuery],
    school_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
) -> AttentionPageOut:
    return AttentionPageOut.model_validate(
        asdict(await query.execute(TeacherAttention(user.id, school_id, limit, cursor)))
    )


@router.post("/publications", status_code=201, response_model=PublishOut)
async def publish(
    body: PublishIn, user: CurrentUser, handler: FromDishka[PublishHandler]
) -> PublishOut:
    run = NewRun(
        RunMode(body.run.mode), body.run.opens_at, body.run.closes_at, body.run.planner_mode
    )
    result = await handler.execute(
        Publish(
            actor_id=user.id,
            version_id=body.mission_version_id,
            class_id=body.class_id,
            run=run,
        )
    )
    return PublishOut(
        publication_id=result.publication_id,
        run_id=result.run_id,
        run_status=result.run_status,
    )


@router.get("/teacher/assignments", response_model=AssignmentsOut)
async def assignments(
    user: CurrentUser, query: FromDishka[TeacherAssignmentsQuery]
) -> AssignmentsOut:
    return AssignmentsOut(items=[AssignmentOut(**vars(a)) for a in await query.execute(user.id)])


@router.get("/teacher/publications", response_model=PublicationsPageOut)
async def publications(
    user: CurrentUser,
    query: FromDishka[TeacherPublicationsQuery],
    class_id: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
) -> PublicationsPageOut:
    page = await query.execute(TeacherPublications(user.id, class_id, limit, cursor))
    return PublicationsPageOut(
        items=[
            PublicationOut(
                id=p.id,
                mission_title=p.mission_title,
                class_id=p.class_id,
                class_name=p.class_name,
                run=RunSummaryOut(**vars(p.run)),
                counts=CountsOut(**vars(p.counts)),
                released_to_parents_at=p.released_to_parents_at,
            )
            for p in page.items
        ],
        next_cursor=page.next_cursor,
    )
