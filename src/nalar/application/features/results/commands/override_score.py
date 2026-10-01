from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, InvalidInput, NotFound
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class OverrideScore:
    actor_id: UUID
    score_id: UUID
    final_level: int
    reason: str


@dataclass(frozen=True)
class Overridden:
    score_id: UUID
    ai_level: int
    final_level: int
    overridden_at: datetime


class OverrideScoreHandler:
    """TC-12: the score_overrides trigger sets final_level; ai_level is never touched."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, cmd: OverrideScore) -> Overridden:
        reason = cmd.reason.strip()
        async with self._uow:
            ref = await self._uow.grading.lock_score(cmd.score_id)
            if ref is None or not await self._uow.authz.teaches_session(
                cmd.actor_id, ref.session_id
            ):
                raise NotFound()
            if not reason:
                raise InvalidInput(details={"reason": "blank"})
            if cmd.final_level == ref.final_level:
                raise Conflict("SCORE_UNCHANGED", "Level baru sama dengan level saat ini.")
            at = await self._uow.grading.insert_override(ref, cmd.actor_id, cmd.final_level, reason)
            await self._uow.audit.record(
                ref.school_id,
                cmd.actor_id,
                "score.overridden",
                "evaluation_scores",
                ref.id,
                {"previous_level": ref.final_level, "new_level": cmd.final_level},
            )
        return Overridden(ref.id, ref.ai_level, cmd.final_level, at)
