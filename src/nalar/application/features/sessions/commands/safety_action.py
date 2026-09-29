from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.features.evaluation.messages import evaluation_message
from nalar.application.features.sessions.fallback_turn import append_fixed_question
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import EVAL_QUEUE
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import SessionStatus


@dataclass(frozen=True)
class SafetyAction:
    actor_id: UUID
    session_id: UUID
    action: Literal["resume", "end"]
    note: str | None


@dataclass(frozen=True)
class SafetyActed:
    status: SessionStatus
    acted_at: datetime


class SafetyActionHandler:
    """Audited; never creates an authenticity flag (contract check 7)."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, cmd: SafetyAction) -> SafetyActed:
        async with self._uow:
            uow = self._uow
            if not await uow.authz.teaches_session(cmd.actor_id, cmd.session_id):
                raise NotFound()
            now = self._clock.now()
            if cmd.action == "end":
                if not await uow.sessions.end_safety(cmd.session_id, now):
                    raise Conflict("SESSION_NOT_PAUSED")
                await uow.queue.send(EVAL_QUEUE, evaluation_message(cmd.session_id))
                status = SessionStatus.ended_safety
            else:
                ref = await uow.sessions.get_ref(cmd.session_id)
                if ref is None or ref.status is not SessionStatus.paused_safety:
                    raise Conflict("SESSION_NOT_PAUSED")
                if now >= ref.deadline_at:
                    raise Conflict("SESSION_DEADLINE_PASSED")
                if not await uow.sessions.resume(cmd.session_id, now):
                    raise Conflict("SESSION_NOT_PAUSED")
                ctx = await uow.sessions.turn_context(cmd.session_id)
                assert ctx is not None
                # D-S10-7: a fixed question instead of re-asking the AI with the same history.
                await append_fixed_question(
                    uow, ctx=ctx, answered=ctx.turns[-1], move_source="fixed_rule", now=now
                )
                status = SessionStatus.in_progress
            await uow.audit.session_action(
                cmd.session_id, cmd.actor_id, f"session.safety_{cmd.action}", {"note": cmd.note}
            )
        return SafetyActed(status, now)
