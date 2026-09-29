from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from nalar.application.errors import TooManyRequests
from nalar.application.features.sessions.commands.join_run import JoinRun, JoinRunHandler
from nalar.application.features.sessions.commands.start_window_session import (
    StartWindowSession,
    StartWindowSessionHandler,
)
from nalar.application.features.sessions.commands.submit_warmup import (
    SubmitWarmup,
    SubmitWarmupHandler,
)
from nalar.application.features.sessions.queries.lobby_state import LobbyStateQuery
from nalar.application.features.sessions.queries.student_missions import StudentMissionsQuery
from nalar.domain.labels import ParticipantStatus
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.rate_limit import RateLimiter
from nalar.presentation.api.schemas.student import (
    JoinIn,
    JoinOut,
    MissionCardOut,
    PromptOut,
    StudentLobbyOut,
    StudentMissionsOut,
    WarmupChoiceIn,
    WarmupChoiceOut,
    WarmupOut,
    WarmupSavedOut,
    WindowSessionOut,
)

router = APIRouter(prefix="/student", tags=["student"], route_class=DishkaRoute)


@router.get("/missions", response_model=StudentMissionsOut)
async def missions(
    user: CurrentUser, query: FromDishka[StudentMissionsQuery]
) -> StudentMissionsOut:
    buckets: dict[str, list[MissionCardOut]] = {"upcoming": [], "open": [], "completed": []}
    for card in await query.execute(user.id):
        buckets[card.bucket].append(
            MissionCardOut(
                publication_id=card.publication_id,
                mission_title=card.mission_title,
                subject_name=card.subject_name,
                mode=card.mode,
                opens_at=card.opens_at,
                closes_at=card.closes_at,
                run_status=card.run_status,
                attempt_status=card.attempt_status,
                max_duration_minutes=card.max_duration_minutes,
            )
        )
    return StudentMissionsOut(**buckets)


@router.post("/runs/join", response_model=JoinOut)
async def join(
    body: JoinIn,
    user: CurrentUser,
    handler: FromDishka[JoinRunHandler],
    limiter: FromDishka[RateLimiter],
) -> JoinOut:
    if not limiter.allow(str(user.id)):
        raise TooManyRequests()
    result = await handler.execute(JoinRun(user.id, body.join_code))
    target, participant = result.target, result.participant
    warmup = target.live_warmup if participant.status is ParticipantStatus.waiting else None
    return JoinOut(
        run_id=target.run_id,
        participant_id=participant.id,
        publication_id=target.publication_id,
        mission_title=target.mission_title,
        run_status=target.status.value,
        session_id=participant.session_id,
        warmup=WarmupOut(
            prompt=str(warmup["prompt"]),
            choices=[
                WarmupChoiceOut(id=str(c["id"]), text=str(c["text"])) for c in warmup["choices"]
            ],
        )
        if warmup
        else None,
        deadline_at=result.deadline_at,
    )


@router.get("/runs/{run_id}/lobby", response_model=StudentLobbyOut)
async def lobby(
    run_id: UUID, user: CurrentUser, query: FromDishka[LobbyStateQuery]
) -> StudentLobbyOut:
    return StudentLobbyOut(**vars(await query.execute(user.id, run_id)))


@router.put("/runs/{run_id}/warmup-choice", response_model=WarmupSavedOut)
async def warmup_choice(
    run_id: UUID, body: WarmupChoiceIn, user: CurrentUser, handler: FromDishka[SubmitWarmupHandler]
) -> WarmupSavedOut:
    saved = await handler.execute(SubmitWarmup(user.id, run_id, body.choice_id))
    return WarmupSavedOut(choice_id=saved.choice_id, submitted_at=saved.submitted_at)


@router.post(
    "/publications/{publication_id}/window-session",
    status_code=201,
    response_model=WindowSessionOut,
)
async def window_session(
    publication_id: UUID, user: CurrentUser, handler: FromDishka[StartWindowSessionHandler]
) -> WindowSessionOut:
    session = await handler.execute(StartWindowSession(user.id, publication_id))
    return WindowSessionOut(
        session_id=session.session_id,
        status=session.status,
        started_at=session.started_at,
        deadline_at=session.deadline_at,
        prompt=PromptOut(kind="anchor", text=session.anchor_text, turn_index=0),
    )
