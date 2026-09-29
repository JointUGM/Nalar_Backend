import json
from datetime import datetime
from uuid import UUID

import asyncpg

from nalar.application.ports.runs import LockedRun, WarmInput
from nalar.domain.labels import RunMode, RunStatus
from nalar.infrastructure.db.pool import DbConnection

# One statement: every waiting participant gets a session, its anchor turn and the started state.
# Foreign keys are checked at the end of the statement, so they see the new sessions.
_START = """
with waiting as (
    select id, student_id from run_participants
     where run_id = $1 and status = 'waiting' for update
), new_sessions as (
    insert into sessions (school_id, publication_id, run_id, student_id, attempt_number,
                          started_at, last_activity_at, deadline_at)
    select $2, $3, $1, w.student_id,
           coalesce((select max(s.attempt_number) from sessions s
                      where s.publication_id = $3 and s.student_id = w.student_id), 0) + 1,
           $4, $4, $5
      from waiting w
    returning id, student_id
), anchors as (
    insert into session_turns (school_id, session_id, turn_index, prompt_kind, prompt_text,
                               prompt_shown_at)
    select $2, ns.id, 0, 'anchor', $6, $4 from new_sessions ns
    returning session_id
)
update run_participants p set status = 'started', session_id = ns.id
  from new_sessions ns
 where p.run_id = $1 and p.student_id = ns.student_id
returning p.id
"""


class PgRunsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def lock(self, run_id: UUID) -> LockedRun | None:
        row = await self._conn.fetchrow(
            "select r.id, r.school_id, r.publication_id, r.mode::text as mode,"
            " r.status::text as status, r.join_code, r.lobby_opened_at, r.started_at,"
            " r.closed_at, mv.anchor_problem, mv.max_duration_minutes"
            " from publication_runs r join publications p on p.id = r.publication_id"
            " join mission_versions mv on mv.id = p.mission_version_id"
            " where r.id = $1 for update of r",
            run_id,
        )
        if row is None:
            return None
        data = dict(row)
        return LockedRun(
            **{**data, "mode": RunMode(data["mode"]), "status": RunStatus(data["status"])}
        )

    async def open_lobby(self, run_id: UUID, join_code: str, now: datetime) -> bool:
        # A savepoint: a code clash rolls back only this update, not the caller's transaction.
        try:
            async with self._conn.transaction():
                await self._conn.execute(
                    "update publication_runs set status = 'lobby', join_code = $2,"
                    " lobby_opened_at = $3 where id = $1",
                    run_id,
                    join_code,
                    now,
                )
        except asyncpg.UniqueViolationError:
            return False
        return True

    async def start(self, run: LockedRun, now: datetime, deadline: datetime) -> int:
        await self._conn.execute(
            "update publication_runs set status = 'open', started_at = $2 where id = $1",
            run.id,
            now,
        )
        rows = await self._conn.fetch(
            _START, run.id, run.school_id, run.publication_id, now, deadline, run.anchor_problem
        )
        return len(rows)

    async def started_count(self, run_id: UUID) -> int:
        count: int = await self._conn.fetchval(
            "select count(*) from run_participants where run_id = $1 and status = 'started'",
            run_id,
        )
        return count

    async def close(self, run_id: UUID, now: datetime) -> None:
        await self._conn.execute(
            "update run_participants set status = 'cancelled'"
            " where run_id = $1 and status = 'waiting'",
            run_id,
        )
        await self._conn.execute(
            "update publication_runs set status = 'closed', closed_at = $2 where id = $1",
            run_id,
            now,
        )

    async def warm_input(self, run_id: UUID) -> WarmInput | None:
        row = await self._conn.fetchrow(
            "select r.school_id, mv.context_pack from publication_runs r"
            " join publications p on p.id = r.publication_id"
            " join mission_versions mv on mv.id = p.mission_version_id where r.id = $1",
            run_id,
        )
        if row is None or row["context_pack"] is None:
            return None
        return WarmInput(row["school_id"], json.loads(row["context_pack"]))
