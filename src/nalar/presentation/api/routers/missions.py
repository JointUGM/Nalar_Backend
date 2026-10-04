from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Header, Query

from nalar.application.features.missions.commands.create_mission import (
    CreateMission,
    CreateMissionHandler,
)
from nalar.application.features.missions.commands.create_version import (
    CreateVersion,
    CreateVersionHandler,
)
from nalar.application.features.missions.commands.generate_mission import GenerateMissionHandler
from nalar.application.features.missions.commands.review_version import ReviewVersionHandler
from nalar.application.features.missions.queries.get_version import GetVersionQuery
from nalar.application.features.missions.queries.list_missions import (
    ListMissions,
    ListMissionsQuery,
)
from nalar.application.features.missions.queries.list_versions import ListVersionsQuery
from nalar.application.ports.missions import VersionDraft
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.missions import (
    MissionCreatedOut,
    MissionGenerationQueuedOut,
    MissionIn,
    MissionPageOut,
    MissionSummaryOut,
    VersionCreatedOut,
    VersionHistoryOut,
    VersionIn,
    VersionOut,
    VersionReviewedOut,
    VersionSummaryOut,
)

router = APIRouter(tags=["missions"], route_class=DishkaRoute)


@router.get("/missions/{mission_id}/versions", response_model=list[VersionHistoryOut])
async def list_versions(
    mission_id: UUID, user: CurrentUser, query: FromDishka[ListVersionsQuery]
) -> list[VersionHistoryOut]:
    return [VersionHistoryOut(**asdict(v)) for v in await query.execute(user.id, mission_id)]


@router.post("/missions", status_code=201, response_model=MissionCreatedOut)
async def create_mission(
    body: MissionIn,
    user: CurrentUser,
    handler: FromDishka[CreateMissionHandler],
    idempotency_key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> MissionCreatedOut:
    mission_id = await handler.execute(
        CreateMission(
            user.id, body.knowledge_base_id, body.title, body.learning_objective, idempotency_key
        )
    )
    return MissionCreatedOut(mission_id=mission_id)


@router.post(
    "/missions/{mission_id}/generate", status_code=202, response_model=MissionGenerationQueuedOut
)
async def generate_mission(
    mission_id: UUID, user: CurrentUser, handler: FromDishka[GenerateMissionHandler]
) -> MissionGenerationQueuedOut:
    queued = await handler.execute(user.id, mission_id)
    return MissionGenerationQueuedOut(job_id=queued.job_id)


@router.get("/schools/{school_id}/missions", response_model=MissionPageOut)
async def list_missions(
    school_id: UUID,
    user: CurrentUser,
    query: FromDishka[ListMissionsQuery],
    school_subject_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    q: str = Query("", max_length=120),
    status: Literal["draft", "reviewed", "locked", "archived"] | None = None,
) -> MissionPageOut:
    page = await query.execute(
        ListMissions(user.id, school_id, school_subject_id, limit, cursor, q, status)
    )
    return MissionPageOut(
        items=[
            MissionSummaryOut(
                id=m.id,
                title=m.title,
                knowledge_base_id=m.knowledge_base_id,
                created_by=m.created_by,
                created_by_name=m.created_by_name,
                latest_version=VersionSummaryOut(**asdict(m.latest_version))
                if m.latest_version
                else None,
                can_edit=m.created_by == user.id,
            )
            for m in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.post("/missions/{mission_id}/versions", status_code=201, response_model=VersionCreatedOut)
async def create_version(
    mission_id: UUID, body: VersionIn, user: CurrentUser, handler: FromDishka[CreateVersionHandler]
) -> VersionCreatedOut:
    draft = VersionDraft(
        anchor_problem=body.anchor_problem,
        rubric=body.rubric.model_dump(),
        target_concept_ids=tuple(body.target_concept_ids),
        misconception_ids=tuple(body.misconception_ids),
        question_bank=tuple(q.model_dump(mode="json") for q in body.question_bank),
        answer_terms=tuple(body.answer_terms),
        reference_reasoning=body.reference_reasoning,
        source_chunk_ids=tuple(body.source_chunk_ids),
        live_warmup=body.live_warmup.model_dump() if body.live_warmup else None,
        max_turns=body.max_turns,
        max_duration_minutes=body.max_duration_minutes,
    )
    done = await handler.execute(CreateVersion(user.id, mission_id, draft))
    return VersionCreatedOut(version_id=done.version_id, version_number=done.version_number)


@router.get("/missions/{mission_id}/versions/{number}", response_model=VersionOut)
async def get_version(
    mission_id: UUID, number: int, user: CurrentUser, query: FromDishka[GetVersionQuery]
) -> VersionOut:
    v, can_edit = await query.execute(user.id, mission_id, number)
    d = v.draft
    return VersionOut(
        id=v.id,
        version_number=v.version_number,
        status=v.status,
        anchor_problem=d.anchor_problem,
        rubric={k: list(x) for k, x in d.rubric.items()},
        target_concept_ids=list(d.target_concept_ids),
        misconception_ids=list(d.misconception_ids),
        question_bank=[dict(q) for q in d.question_bank],
        answer_terms=list(d.answer_terms),
        reference_reasoning=d.reference_reasoning,
        source_chunk_ids=list(d.source_chunk_ids),
        live_warmup=dict(d.live_warmup) if d.live_warmup else None,
        max_turns=d.max_turns,
        max_duration_minutes=d.max_duration_minutes,
        can_edit=can_edit,
    )


@router.post("/missions/{mission_id}/versions/{number}/review", response_model=VersionReviewedOut)
async def review_version(
    mission_id: UUID, number: int, user: CurrentUser, handler: FromDishka[ReviewVersionHandler]
) -> VersionReviewedOut:
    done = await handler.execute(user.id, mission_id, number)
    return VersionReviewedOut(version_id=done.version_id, reviewed_at=done.reviewed_at)
