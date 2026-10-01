from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class ScoreRef:
    id: UUID
    session_id: UUID
    school_id: UUID
    ai_level: int
    final_level: int


@dataclass(frozen=True)
class FlagRef:
    id: UUID
    session_id: UUID
    school_id: UUID
    status: str
    reviewed_at: datetime | None


class GradingRepo(Protocol):
    async def lock_score(self, score_id: UUID) -> ScoreRef | None:
        """The score row locked FOR UPDATE, so two overrides can't read the same previous level."""
        ...

    async def insert_override(
        self, ref: ScoreRef, actor_id: UUID, new_level: int, reason: str
    ) -> datetime: ...

    async def flag_ref(self, flag_id: UUID) -> FlagRef | None: ...

    async def review_flag(
        self, flag_id: UUID, actor_id: UUID, decision: str, note: str | None, now: datetime
    ) -> bool:
        """False when the flag was no longer open."""
        ...
