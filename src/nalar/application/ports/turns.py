from datetime import datetime
from typing import Protocol
from uuid import UUID


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
