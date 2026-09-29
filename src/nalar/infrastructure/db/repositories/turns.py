from datetime import datetime
from uuid import UUID

from nalar.infrastructure.db.pool import DbConnection


class PgTurnsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def save_answer(
        self,
        session_id: UUID,
        turn_index: int,
        answer_text: str,
        submission_id: UUID,
        now: datetime,
    ) -> bool:
        # NFR-R1: the row lock plus "answer_text is null" lets the first writer win; a racer
        # re-checks the condition after the winner commits and updates nothing.
        row = await self._conn.fetchrow(
            "update session_turns t set answer_text = $3, client_submission_id = $4,"
            " answer_submitted_at = $5 from sessions s"
            " where t.session_id = $1 and t.turn_index = $2 and t.answer_text is null"
            " and s.id = t.session_id and s.status = 'in_progress'"
            " and s.current_turn_index = $2 and s.deadline_at > $5 returning t.id",
            session_id,
            turn_index,
            answer_text,
            submission_id,
            now,
        )
        return row is not None

    async def submission_turn(self, session_id: UUID, submission_id: UUID) -> int | None:
        turn_index: int | None = await self._conn.fetchval(
            "select turn_index from session_turns"
            " where session_id = $1 and client_submission_id = $2",
            session_id,
            submission_id,
        )
        return turn_index

    async def is_answered(self, session_id: UUID, turn_index: int) -> bool:
        return bool(
            await self._conn.fetchval(
                "select exists (select 1 from session_turns where session_id = $1"
                " and turn_index = $2 and answer_text is not null)",
                session_id,
                turn_index,
            )
        )
