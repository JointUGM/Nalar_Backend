from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class TurnAnalysis:
    answer_state: str | None
    detected_misconception_id: UUID | None
    secondary_misconception_id: UUID | None
    frustration: bool
    safety_paused: bool
    ai_invocation_id: UUID | None


@dataclass(frozen=True)
class NewTurn:
    turn_index: int
    prompt_text: str
    move: str
    move_source: str
    move_reason_code: str
    move_reason: str | None
    guard_result: str
    question_bank_id: str
    target_concept_id: UUID
    allowed_moves: tuple[str, ...] | None
    ai_invocation_id: UUID | None
    shown_at: datetime


class TurnsRepo(Protocol):
    async def save_answer(
        self,
        session_id: UUID,
        turn_index: int,
        answer_text: str,
        submission_id: UUID,
        now: datetime,
    ) -> bool:
        """Save the answer on the open turn of an in-progress session before its deadline."""
        ...

    async def submission_turn(self, session_id: UUID, submission_id: UUID) -> int | None: ...

    async def is_answered(self, session_id: UUID, turn_index: int) -> bool: ...

    async def record_analysis(self, turn_id: UUID, analysis: TurnAnalysis) -> None: ...

    async def exists(self, session_id: UUID, turn_index: int) -> bool: ...

    async def append(self, session_id: UUID, school_id: UUID, turn: NewTurn) -> bool:
        """Insert the next turn once (unique turn index) and advance current_turn_index."""
        ...
