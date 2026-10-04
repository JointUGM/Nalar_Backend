from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, Forbidden, NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.participants import JoinTarget, Participant
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.join_code import normalize
from nalar.domain.labels import ParticipantStatus, RunStatus
from nalar.domain.sessions import deadline_at


@dataclass(frozen=True)
class JoinRun:
    actor_id: UUID
    join_code: str


@dataclass(frozen=True)
class JoinResult:
    target: JoinTarget
    participant: Participant
    deadline_at: datetime | None


class JoinRunHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, cmd: JoinRun) -> JoinResult:
        code = normalize(cmd.join_code)
        if code is None:
            raise NotFound("JOIN_CODE_INVALID")
        async with self._uow:
            uow, actor = self._uow, cmd.actor_id
            target = await uow.participants.joinable_by_code(code)
            if target is None:
                closed = await uow.participants.latest_closed_by_code(code)
                if closed is not None and await uow.authz.is_enrolled(actor, closed.class_id):
                    raise Conflict("RUN_NOT_JOINABLE")
                raise NotFound("JOIN_CODE_INVALID")
            if not await uow.authz.is_enrolled(actor, target.class_id):
                # NFR-S9: only a student of the same school learns that the code exists.
                if await uow.authz.is_school_student(actor, target.school_id):
                    raise Forbidden("NOT_ENROLLED")
                raise NotFound("JOIN_CODE_INVALID")
            existing = await uow.participants.get(target.run_id, actor)
            if existing is not None:
                if existing.status is ParticipantStatus.cancelled:
                    raise Conflict("PARTICIPANT_CANCELLED")
                return await self._result(target, existing)
            if await uow.sessions.latest_for_student(target.publication_id, actor) is not None:
                raise Conflict("ATTEMPT_ALREADY_USED")
            now = self._clock.now()
            if target.status is RunStatus.lobby:
                participant = await uow.participants.insert_waiting(
                    target.school_id, target.run_id, actor, now
                )
                return await self._result(target, participant)
            deadline = deadline_at(now, target.max_duration_minutes)
            session_id = await uow.sessions.create_with_anchor(
                school_id=target.school_id,
                publication_id=target.publication_id,
                run_id=target.run_id,
                student_id=actor,
                attempt_number=1,
                started_at=now,
                deadline_at=deadline,
                anchor_text=target.anchor_problem,
            )
            if session_id is None:
                raced = await uow.participants.get(target.run_id, actor)
                if raced is None:
                    raise Conflict("ATTEMPT_ALREADY_USED")
                return await self._result(target, raced)
            participant = await uow.participants.insert_started(
                target.school_id, target.run_id, actor, session_id, now
            )
            return JoinResult(target, participant, deadline)

    async def _result(self, target: JoinTarget, participant: Participant) -> JoinResult:
        session = (
            await self._uow.sessions.get_ref(participant.session_id)
            if participant.session_id
            else None
        )
        return JoinResult(target, participant, session.deadline_at if session else None)
