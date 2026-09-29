from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.sessions import SessionRef
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import RunMode, RunStatus, SessionStatus
from nalar.domain.sessions import deadline_at


@dataclass(frozen=True)
class StartWindowSession:
    actor_id: UUID
    publication_id: UUID


@dataclass(frozen=True)
class WindowSession:
    session_id: UUID
    status: SessionStatus
    started_at: datetime
    deadline_at: datetime
    anchor_text: str


class StartWindowSessionHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, cmd: StartWindowSession) -> WindowSession:
        async with self._uow:
            uow, actor = self._uow, cmd.actor_id
            target = await uow.participants.window_target(cmd.publication_id)
            if target is None or not await uow.authz.is_enrolled(actor, target.class_id):
                raise NotFound()
            if target.mode is not RunMode.window or target.status is not RunStatus.open:
                raise Conflict("RUN_STATE_CONFLICT", details={"status": target.status.value})
            anchor = target.anchor_problem
            mine = await uow.sessions.for_run(target.run_id, actor)
            if mine is not None:
                return _window(mine, anchor)
            if await uow.sessions.latest_for_student(target.publication_id, actor) is not None:
                raise Conflict("ATTEMPT_ALREADY_USED")
            now = self._clock.now()
            deadline = deadline_at(now, target.max_duration_minutes)
            session_id = await uow.sessions.create_with_anchor(
                school_id=target.school_id,
                publication_id=target.publication_id,
                run_id=target.run_id,
                student_id=actor,
                attempt_number=1,
                started_at=now,
                deadline_at=deadline,
                anchor_text=anchor,
            )
            if session_id is None:
                raced = await uow.sessions.for_run(target.run_id, actor)
                if raced is None:
                    raise Conflict("ATTEMPT_ALREADY_USED")
                return _window(raced, anchor)
            return WindowSession(session_id, SessionStatus.in_progress, now, deadline, anchor)


def _window(session: SessionRef, anchor: str) -> WindowSession:
    return WindowSession(
        session.id, session.status, session.started_at, session.deadline_at, anchor
    )
