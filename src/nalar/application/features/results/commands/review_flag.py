from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ReviewFlag:
    actor_id: UUID
    flag_id: UUID
    decision: Literal["cleared", "concern_confirmed"]
    note: str | None


@dataclass(frozen=True)
class FlagReviewed:
    status: str
    reviewed_at: datetime


class ReviewFlagHandler:
    """TC-11: a human decision, never a verdict. Repeating the same decision is a no-op."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, cmd: ReviewFlag) -> FlagReviewed:
        async with self._uow:
            ref = await self._uow.grading.flag_ref(cmd.flag_id)
            if ref is None or not await self._uow.authz.teaches_session(
                cmd.actor_id, ref.session_id
            ):
                raise NotFound()
            now = self._clock.now()
            if await self._uow.grading.review_flag(
                cmd.flag_id, cmd.actor_id, cmd.decision, cmd.note, now
            ):
                await self._uow.audit.record(
                    ref.school_id,
                    cmd.actor_id,
                    "flag.reviewed",
                    "authenticity_flags",
                    ref.id,
                    {"decision": cmd.decision, "note": cmd.note},
                )
                return FlagReviewed(cmd.decision, now)
            current = await self._uow.grading.flag_ref(cmd.flag_id)
        assert current is not None and current.reviewed_at is not None
        if current.status != cmd.decision:
            raise Conflict("FLAG_ALREADY_REVIEWED", "Tanda ini sudah ditinjau.")
        return FlagReviewed(current.status, current.reviewed_at)
