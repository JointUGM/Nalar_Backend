from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.features.evaluation.messages import evaluation_message
from nalar.application.features.sessions.timing import TurnTiming
from nalar.application.ports.background import BackgroundWork
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import EVAL_QUEUE
from nalar.application.ports.sessions import StateView
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import SessionStatus
from nalar.domain.sessions import OPEN_STATUSES, SAFETY_MESSAGE


@dataclass(frozen=True)
class Prompt:
    kind: str
    text: str
    turn_index: int


@dataclass(frozen=True)
class StudentState:
    status: str
    turn_index: int
    probe_number: int
    probe_total: int
    started_at: datetime
    deadline_at: datetime
    prompt: Prompt | None
    safety_message: str | None
    reflection_ready: bool


class SessionStateQuery:
    def __init__(
        self, uow: UnitOfWork, clock: Clock, background: BackgroundWork, timing: TurnTiming
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._background = background
        self._timing = timing

    async def execute(self, actor_id: UUID, session_id: UUID) -> StudentState:
        now = self._clock.now()
        async with self._uow:
            if not await self._uow.authz.owns_session(actor_id, session_id):
                raise NotFound()
            view = await self._uow.sessions.state_view(session_id)
            if view is None:
                raise NotFound()
            if view.status in OPEN_STATUSES and now >= view.deadline_at:
                # NFR-R2: the guarded update lets exactly one reader enqueue the evaluation.
                if await self._uow.sessions.time_out(session_id, now):
                    await self._uow.queue.send(EVAL_QUEUE, evaluation_message(session_id))
                view = await self._uow.sessions.state_view(session_id) or view
        if (
            view.status is SessionStatus.in_progress
            and view.latest_answered_at is not None
            and now - view.latest_answered_at >= self._timing.recovery_after
        ):
            self._background.run_turn_step(session_id, view.latest_turn_index)
        return _student_state(view)


def _student_state(view: StateView) -> StudentState:
    prompt, message = None, None
    if view.status is SessionStatus.in_progress:
        status = "processing" if view.latest_answered_at else "awaiting_answer"
        if status == "awaiting_answer":
            prompt = Prompt(view.latest_kind, view.latest_text, view.latest_turn_index)
    elif view.status is SessionStatus.paused_safety:
        status, message = "paused_safety", SAFETY_MESSAGE
    else:
        status = view.status.value if view.evaluated else "evaluating"
    return StudentState(
        status=status,
        turn_index=view.latest_turn_index,
        probe_number=view.latest_turn_index,
        probe_total=view.max_turns,
        started_at=view.started_at,
        deadline_at=view.deadline_at,
        prompt=prompt,
        safety_message=message,
        reflection_ready=view.reflection_ready,
    )
