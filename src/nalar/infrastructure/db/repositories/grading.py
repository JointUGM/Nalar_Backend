from datetime import datetime
from uuid import UUID

from nalar.application.ports.grading import FlagRef, ScoreRef
from nalar.infrastructure.db.pool import DbConnection


class PgGradingRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def lock_score(self, score_id: UUID) -> ScoreRef | None:
        row = await self._conn.fetchrow(
            "select sc.id, e.session_id, sc.school_id, sc.ai_level, sc.final_level"
            "  from evaluation_scores sc join session_evaluations e on e.id = sc.evaluation_id"
            " where sc.id = $1 for update of sc",
            score_id,
        )
        return ScoreRef(**dict(row)) if row else None

    async def insert_override(
        self, ref: ScoreRef, actor_id: UUID, new_level: int, reason: str
    ) -> datetime:
        created_at: datetime = await self._conn.fetchval(
            "insert into score_overrides"
            " (school_id, score_id, overridden_by, previous_level, new_level, reason)"
            " values ($1, $2, $3, $4, $5, $6) returning created_at",
            ref.school_id,
            ref.id,
            actor_id,
            ref.final_level,
            new_level,
            reason,
        )
        return created_at

    async def flag_ref(self, flag_id: UUID) -> FlagRef | None:
        row = await self._conn.fetchrow(
            "select id, session_id, school_id, status::text as status, reviewed_at"
            "  from authenticity_flags where id = $1",
            flag_id,
        )
        return FlagRef(**dict(row)) if row else None

    async def review_flag(
        self, flag_id: UUID, actor_id: UUID, decision: str, note: str | None, now: datetime
    ) -> bool:
        return (
            await self._conn.fetchval(
                "update authenticity_flags set status = $2::flag_status, reviewed_by = $3,"
                " reviewed_at = $4, review_note = $5 where id = $1 and status = 'open'"
                " returning id",
                flag_id,
                decision,
                actor_id,
                now,
                note,
            )
            is not None
        )
