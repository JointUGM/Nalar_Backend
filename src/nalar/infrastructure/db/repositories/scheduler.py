from datetime import datetime
from uuid import UUID

from nalar.infrastructure.db.pool import DbConnection

_STUCK_TURNS = """
    select t.session_id, t.turn_index
      from session_turns t
      join sessions s on s.id = t.session_id
     where s.status = 'in_progress' and t.turn_index = s.current_turn_index
       and t.answer_text is not null and t.answer_submitted_at <= $1
"""

# D-S10-8: a queued message means "in hand"; an archived one means "needs a human".
_UNEVALUATED = """
    select s.id from sessions s
     where s.status in ('completed', 'timed_out', 'ended_safety') and s.ended_at <= $1
       and not exists (select 1 from session_evaluations e where e.session_id = s.id)
       and not exists (select 1 from pgmq.q_nalar_eval q
                        where q.message->>'session_id' = s.id::text)
       and not exists (select 1 from pgmq.a_nalar_eval a
                        where a.message->>'session_id' = s.id::text)
     order by s.ended_at
     limit 200
"""


_TO_FINALIZE = """
    with finalized as (
        select entity_id, count(*) filter (where status = 'succeeded') as runs,
               bool_or(status in ('queued', 'running') or updated_at > $1) as busy
          from jobs where kind = 'publication_finalize' group by entity_id)
    select p.id, p.school_id from publications p
      left join finalized f on f.entity_id = p.id
     where p.released_to_parents_at is null and p.cancelled_at is null
       and not coalesce(f.busy, false)
       and exists (select 1 from sessions s where s.publication_id = p.id)
       and not exists (select 1 from publication_runs r
                        where r.publication_id = p.id and r.status <> 'closed')
       and not exists (select 1 from sessions s where s.publication_id = p.id
                         and (s.status in ('in_progress', 'paused_safety')
                              or not exists (select 1 from session_evaluations e
                                              where e.session_id = s.id)))
       and (coalesce(f.runs, 0) = 0
            or exists (select 1 from v_latest_sessions ls
                         join session_evaluations e on e.session_id = ls.id
                          and e.status = 'completed'
                        where ls.publication_id = p.id and ls.status = 'completed'
                          and (not exists (select 1 from parent_summaries ps
                                            where ps.publication_id = p.id
                                              and ps.student_id = ls.student_id)
                               or (f.runs < 2 and not exists (select 1 from class_map_insights i
                                                               where i.publication_id = p.id)))))
     order by p.created_at
     limit 50
"""


class PgSchedulerRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def _ids(self, sql: str, *args: object) -> list[UUID]:
        return [row[0] for row in await self._conn.fetch(sql, *args)]

    async def open_due_windows(self, now: datetime) -> list[UUID]:
        return await self._ids(
            "update publication_runs set status = 'open' where mode = 'window'"
            " and status = 'scheduled' and opens_at <= $1 and closes_at > $1 returning id",
            now,
        )

    async def close_due_windows(self, now: datetime) -> list[UUID]:
        return await self._ids(
            "update publication_runs set status = 'closed', closed_at = $1 where mode = 'window'"
            " and status in ('scheduled', 'open') and closes_at <= $1 returning id",
            now,
        )

    async def time_out_overdue(self, now: datetime) -> list[UUID]:
        return await self._ids(
            "update sessions set status = 'timed_out', end_reason = 'max_duration_reached',"
            " ended_at = deadline_at where status in ('in_progress', 'paused_safety')"
            " and deadline_at <= $1 returning id",
            now,
        )

    async def stuck_turns(self, answered_before: datetime) -> list[tuple[UUID, int]]:
        rows = await self._conn.fetch(_STUCK_TURNS, answered_before)
        return [(r["session_id"], r["turn_index"]) for r in rows]

    async def unevaluated_sessions(self, ended_before: datetime) -> list[UUID]:
        return await self._ids(_UNEVALUATED, ended_before)

    async def publications_to_finalize(self, cooldown_before: datetime) -> list[tuple[UUID, UUID]]:
        rows = await self._conn.fetch(_TO_FINALIZE, cooldown_before)
        return [(r["id"], r["school_id"]) for r in rows]
