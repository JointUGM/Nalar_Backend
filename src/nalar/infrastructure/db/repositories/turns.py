from datetime import datetime
from uuid import UUID

from nalar.application.ports.turns import NewTurn, TurnAnalysis
from nalar.infrastructure.db.pool import DbConnection

_APPEND = """
    insert into session_turns (school_id, session_id, turn_index, prompt_kind, prompt_strategy,
                               prompt_text, prompt_shown_at, prompt_ai_invocation_id,
                               target_concept_id, allowed_moves, move_source, move_reason_code,
                               move_reason, question_bank_id, guard_result)
    values ($1, $2, $3, 'probe', $4::probe_strategy, $5, $6, $7, $8,
            $9::text[]::probe_strategy[], $10::move_source, $11::move_reason_code, $12, $13,
            $14::guard_result)
    on conflict (session_id, turn_index) do nothing
    returning id
"""


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

    async def record_analysis(self, turn_id: UUID, analysis: TurnAnalysis) -> None:
        await self._conn.execute(
            "update session_turns set answer_state = $2::answer_state,"
            " detected_misconception_id = $3, secondary_misconception_id = $4,"
            " frustration_signal = $5, safety_paused = $6, analysis_ai_invocation_id = $7"
            " where id = $1",
            turn_id,
            analysis.answer_state,
            analysis.detected_misconception_id,
            analysis.secondary_misconception_id,
            analysis.frustration,
            analysis.safety_paused,
            analysis.ai_invocation_id,
        )

    async def exists(self, session_id: UUID, turn_index: int) -> bool:
        return bool(
            await self._conn.fetchval(
                "select exists (select 1 from session_turns"
                " where session_id = $1 and turn_index = $2)",
                session_id,
                turn_index,
            )
        )

    async def append(self, session_id: UUID, school_id: UUID, turn: NewTurn) -> bool:
        new_id = await self._conn.fetchval(
            _APPEND,
            school_id,
            session_id,
            turn.turn_index,
            turn.move,
            turn.prompt_text,
            turn.shown_at,
            turn.ai_invocation_id,
            turn.target_concept_id,
            list(turn.allowed_moves) if turn.allowed_moves is not None else None,
            turn.move_source,
            turn.move_reason_code,
            turn.move_reason,
            turn.question_bank_id,
            turn.guard_result,
        )
        if new_id is None:
            return False
        # NFR-P4: the teacher monitor's Realtime subscription sees this in the same commit.
        await self._conn.execute(
            "update sessions set current_turn_index = $2 where id = $1", session_id, turn.turn_index
        )
        return True
