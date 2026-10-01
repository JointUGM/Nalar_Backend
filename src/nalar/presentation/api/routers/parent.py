from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query

from nalar.application.features.parents.commands.set_preferences import SetPreferencesHandler
from nalar.application.features.parents.queries.children import ChildrenQuery
from nalar.application.features.parents.queries.preferences import PreferencesQuery
from nalar.application.features.parents.queries.progress import ProgressQuery
from nalar.application.features.parents.queries.reflections import ParentReflectionsQuery
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.parent import (
    ParentChildOut,
    ParentChildrenOut,
    ParentPreferencesIn,
    ParentPreferencesOut,
    ParentProgressOut,
    ParentReflectionOut,
    ParentReflectionsOut,
    ParentSummaryOut,
)

router = APIRouter(prefix="/parent", tags=["parent"], route_class=DishkaRoute)
Limit = Annotated[int, Query(ge=1, le=100)]


@router.get("/children", response_model=ParentChildrenOut)
async def children(
    user: CurrentUser,
    query: FromDishka[ChildrenQuery],
    limit: Limit = 20,
    cursor: str | None = None,
) -> ParentChildrenOut:
    page = await query.execute(user.id, limit, cursor)
    return ParentChildrenOut(
        items=[ParentChildOut(**asdict(c)) for c in page.items], next_cursor=page.next_cursor
    )


@router.get("/children/{student_id}/progress", response_model=ParentProgressOut)
async def progress(
    student_id: UUID, user: CurrentUser, query: FromDishka[ProgressQuery]
) -> ParentProgressOut:
    p = await query.execute(user.id, student_id)
    return ParentProgressOut(
        sessions_completed=p.sessions_completed,
        concepts_understood=p.concepts_understood,
        concepts_developing=p.concepts_developing,
        summaries=[
            ParentSummaryOut(
                publication_id=r.publication_id,
                mission_title=r.mission_title,
                released_at=r.released_at,
                text=r.summary,
            )
            for r in p.summaries
        ],
    )


@router.get("/children/{student_id}/reflections", response_model=ParentReflectionsOut)
async def reflections(
    student_id: UUID,
    user: CurrentUser,
    query: FromDishka[ParentReflectionsQuery],
    limit: Limit = 20,
    cursor: str | None = None,
) -> ParentReflectionsOut:
    page = await query.execute(user.id, student_id, limit, cursor)
    return ParentReflectionsOut(
        items=[ParentReflectionOut(**asdict(r)) for r in page.items], next_cursor=page.next_cursor
    )


@router.get("/preferences", response_model=ParentPreferencesOut)
async def preferences(
    user: CurrentUser, query: FromDishka[PreferencesQuery]
) -> ParentPreferencesOut:
    return ParentPreferencesOut(weekly_digest_enabled=await query.execute(user.id))


@router.put("/preferences", response_model=ParentPreferencesOut)
async def set_preferences(
    body: ParentPreferencesIn, user: CurrentUser, handler: FromDishka[SetPreferencesHandler]
) -> ParentPreferencesOut:
    enabled = await handler.execute(user.id, body.weekly_digest_enabled)
    return ParentPreferencesOut(weekly_digest_enabled=enabled)
