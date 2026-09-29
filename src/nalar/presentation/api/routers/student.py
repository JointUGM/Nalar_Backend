import json
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from nalar.application.errors import InvalidInput, TooManyRequests
from nalar.application.features.sessions.commands.ingest_telemetry import (
    IngestTelemetry,
    IngestTelemetryHandler,
)
from nalar.application.features.sessions.commands.join_run import JoinRun, JoinRunHandler
from nalar.application.features.sessions.commands.start_window_session import (
    StartWindowSession,
    StartWindowSessionHandler,
)
from nalar.application.features.sessions.commands.submit_answer import (
    SubmitAnswer,
    SubmitAnswerHandler,
)
from nalar.application.features.sessions.commands.submit_warmup import (
    SubmitWarmup,
    SubmitWarmupHandler,
)
from nalar.application.features.sessions.queries.lobby_state import LobbyStateQuery
from nalar.application.features.sessions.queries.reflection import ReflectionQuery
from nalar.application.features.sessions.queries.session_state import SessionStateQuery
from nalar.application.features.sessions.queries.student_missions import StudentMissionsQuery
from nalar.domain.labels import ParticipantStatus
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.rate_limit import RateLimiter
from nalar.presentation.api.schemas.student import (
    AnswerAccepted,
    AnswerIn,
    JoinIn,
    JoinOut,
    MissionCardOut,
    OpeningGuessOut,
    PromptOut,
    ReflectionOut,
    StateOut,
    StudentLobbyOut,
    StudentMissionsOut,
    TelemetryAccepted,
    TelemetryIn,
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


@router.post("/sessions/{session_id}/answers", status_code=202, response_model=AnswerAccepted)
async def submit_answer(
    session_id: UUID, body: AnswerIn, user: CurrentUser, handler: FromDishka[SubmitAnswerHandler]
) -> AnswerAccepted:
    text = body.answer_text.strip()
    if not text:
        raise InvalidInput(details={"answer_text": "empty"})
    await handler.execute(
        SubmitAnswer(user.id, session_id, body.turn_index, text, body.client_submission_id)
    )
    return AnswerAccepted(next_prompt_url=f"/api/v1/student/sessions/{session_id}/state")


@router.get("/sessions/{session_id}/state", response_model=StateOut)
async def session_state(
    session_id: UUID, user: CurrentUser, query: FromDishka[SessionStateQuery]
) -> StateOut:
    state = await query.execute(user.id, session_id)
    prompt = state.prompt
    return StateOut(
        status=state.status,
        turn_index=state.turn_index,
        probe_number=state.probe_number,
        probe_total=state.probe_total,
        started_at=state.started_at,
        deadline_at=state.deadline_at,
        prompt=PromptOut(kind=prompt.kind, text=prompt.text, turn_index=prompt.turn_index)
        if prompt
        else None,
        safety_message=state.safety_message,
        reflection_ready=state.reflection_ready,
    )


@router.post("/sessions/{session_id}/telemetry", response_model=TelemetryAccepted)
async def telemetry(
    session_id: UUID,
    body: TelemetryIn,
    user: CurrentUser,
    handler: FromDishka[IngestTelemetryHandler],
) -> TelemetryAccepted:
    events_json = json.dumps([e.model_dump(mode="json") for e in body.events])
    seq = await handler.execute(
        IngestTelemetry(
            user.id, session_id, body.client_seq, body.turn_index, events_json, body.client_sent_at
        )
    )
    return TelemetryAccepted(accepted_client_seq=seq)


@router.get(
    "/sessions/{session_id}/reflection",
    response_model=ReflectionOut,
    responses={202: {"description": "Evaluation still pending"}},
)
async def reflection(
    session_id: UUID, user: CurrentUser, query: FromDishka[ReflectionQuery]
) -> ReflectionOut | JSONResponse:
    found = await query.execute(user.id, session_id)
    if found is None:
        return JSONResponse({"status": "pending"}, status_code=202)
    guess = found.opening_guess
    return ReflectionOut(
        mission_title=found.mission_title,
        completed_at=found.completed_at,
        content=found.content,
        opening_guess=OpeningGuessOut(choice_id=guess.choice_id, text=guess.text)
        if guess
        else None,
    )
