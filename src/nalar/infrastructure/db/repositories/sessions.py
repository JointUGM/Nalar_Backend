import json
from datetime import datetime
from uuid import UUID

import asyncpg

from nalar.application.ports.sessions import (
    SessionRef,
    StateView,
    StoredTurn,
    StudentMissionRow,
    TurnContext,
)
from nalar.domain.labels import RunMode, RunStatus, SessionEndReason, SessionStatus
from nalar.infrastructure.db.pool import DbConnection

_REF = "select id, run_id, status::text as status, started_at, deadline_at from sessions"

_TURN_CONTEXT = """
    select s.id as session_id, s.school_id, s.publication_id, s.status::text as status,
           s.started_at, s.deadline_at, mv.max_turns, mv.context_pack,
           r.planner_mode::text as planner_mode
      from sessions s
      join publications p on p.id = s.publication_id
      join mission_versions mv on mv.id = p.mission_version_id
      join publication_runs r on r.id = s.run_id
     where s.id = $1
"""

_STORED_TURNS = """
    select id, turn_index, prompt_kind::text as kind, prompt_text as question_text, answer_text,
           answer_state::text as answer_state, prompt_strategy::text as move, target_concept_id,
           question_bank_id, detected_misconception_id, secondary_misconception_id
      from session_turns where session_id = $1 order by turn_index
"""

_STUDENT_MISSIONS = """
    select p.id as publication_id, mi.title as mission_title, ss.name as subject_name,
           r.mode::text as mode, r.opens_at, r.closes_at, r.status::text as run_status,
           (select s.status::text from sessions s
             where s.publication_id = p.id and s.student_id = $1
             order by s.attempt_number desc limit 1) as latest_status,
           mv.max_duration_minutes
      from class_enrollments ce
      join school_memberships m on m.school_id = ce.school_id and m.user_id = ce.student_id
       and m.role = 'student' and m.status = 'active'
      join publications p on p.class_id = ce.class_id and p.cancelled_at is null
      join publication_runs r on r.publication_id = p.id and r.kind = 'primary'
      join mission_versions mv on mv.id = p.mission_version_id
      join missions mi on mi.id = mv.mission_id
      join knowledge_bases kb on kb.id = mi.knowledge_base_id
      join school_subjects ss on ss.id = kb.school_subject_id
     where ce.student_id = $1 and ce.status = 'active'
     order by coalesce(r.opens_at, p.created_at) desc
"""

_STATE_VIEW = """
    select s.status::text as status, s.started_at, s.deadline_at, mv.max_turns,
           t.turn_index as latest_turn_index, t.prompt_kind::text as latest_kind,
           t.prompt_text as latest_text, t.answer_submitted_at as latest_answered_at,
           exists (select 1 from session_evaluations e where e.session_id = s.id) as evaluated,
           exists (select 1 from session_reflections r where r.session_id = s.id)
             as reflection_ready
      from sessions s
      join publications p on p.id = s.publication_id
      join mission_versions mv on mv.id = p.mission_version_id
      join lateral (select * from session_turns st where st.session_id = s.id
                     order by st.turn_index desc limit 1) t on true
     where s.id = $1
"""


def _ref(row: asyncpg.Record | None) -> SessionRef | None:
    if row is None:
        return None
    data = dict(row)
    return SessionRef(**{**data, "status": SessionStatus(data["status"])})


def _mission_row(row: asyncpg.Record) -> StudentMissionRow:
    data = dict(row)
    latest = data["latest_status"]
    return StudentMissionRow(
        **{
            **data,
            "mode": RunMode(data["mode"]),
            "run_status": RunStatus(data["run_status"]),
            "latest_status": SessionStatus(latest) if latest else None,
        }
    )


class PgSessionsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def create_with_anchor(
        self,
        *,
        school_id: UUID,
        publication_id: UUID,
        run_id: UUID,
        student_id: UUID,
        attempt_number: int,
        started_at: datetime,
        deadline_at: datetime,
        anchor_text: str,
    ) -> UUID | None:
        session_id: UUID | None = await self._conn.fetchval(
            "insert into sessions (school_id, publication_id, run_id, student_id, attempt_number,"
            " started_at, last_activity_at, deadline_at) values ($1, $2, $3, $4, $5, $6, $6, $7)"
            " on conflict (publication_id, student_id, attempt_number) do nothing returning id",
            school_id,
            publication_id,
            run_id,
            student_id,
            attempt_number,
            started_at,
            deadline_at,
        )
        if session_id is not None:
            await self._conn.execute(
                "insert into session_turns (school_id, session_id, turn_index, prompt_kind,"
                " prompt_text, prompt_shown_at) values ($1, $2, 0, 'anchor', $3, $4)",
                school_id,
                session_id,
                anchor_text,
                started_at,
            )
        return session_id

    async def latest_for_student(self, publication_id: UUID, student_id: UUID) -> SessionRef | None:
        return _ref(
            await self._conn.fetchrow(
                _REF + " where publication_id = $1 and student_id = $2"
                " order by attempt_number desc limit 1",
                publication_id,
                student_id,
            )
        )

    async def for_run(self, run_id: UUID, student_id: UUID) -> SessionRef | None:
        return _ref(
            await self._conn.fetchrow(
                _REF
                + " where run_id = $1 and student_id = $2 order by attempt_number desc limit 1",
                run_id,
                student_id,
            )
        )

    async def get_ref(self, session_id: UUID) -> SessionRef | None:
        return _ref(await self._conn.fetchrow(_REF + " where id = $1", session_id))

    async def student_mission_rows(self, student_id: UUID) -> list[StudentMissionRow]:
        return [_mission_row(r) for r in await self._conn.fetch(_STUDENT_MISSIONS, student_id)]

    async def touch(self, session_id: UUID, now: datetime) -> None:
        await self._conn.execute(
            "update sessions set last_activity_at = $2 where id = $1", session_id, now
        )

    async def time_out(self, session_id: UUID, now: datetime) -> bool:
        row = await self._conn.fetchrow(
            "update sessions set status = 'timed_out', end_reason = 'max_duration_reached',"
            " ended_at = deadline_at where id = $1 and status in ('in_progress', 'paused_safety')"
            " and deadline_at <= $2 returning id",
            session_id,
            now,
        )
        return row is not None

    async def state_view(self, session_id: UUID) -> StateView | None:
        row = await self._conn.fetchrow(_STATE_VIEW, session_id)
        if row is None:
            return None
        return StateView(**{**dict(row), "status": SessionStatus(row["status"])})

    async def turn_context(self, session_id: UUID) -> TurnContext | None:
        row = await self._conn.fetchrow(_TURN_CONTEXT, session_id)
        if row is None:
            return None
        turns = await self._conn.fetch(_STORED_TURNS, session_id)
        data = dict(row)
        return TurnContext(
            **{
                **data,
                "status": SessionStatus(data["status"]),
                "context_pack": json.loads(data["context_pack"]),
            },
            turns=tuple(StoredTurn(**dict(t)) for t in turns),
        )

    async def lock_if_in_progress(self, session_id: UUID) -> bool:
        return bool(
            await self._conn.fetchval(
                "select true from sessions where id = $1 and status = 'in_progress' for update",
                session_id,
            )
        )

    async def end(
        self, session_id: UUID, status: SessionStatus, reason: SessionEndReason, now: datetime
    ) -> bool:
        row = await self._conn.fetchrow(
            "update sessions set status = $2::session_status, end_reason = $3::session_end_reason,"
            " ended_at = $4, last_activity_at = $4 where id = $1 and status = 'in_progress'"
            " returning id",
            session_id,
            status.value,
            reason.value,
            now,
        )
        return row is not None

    async def pause_for_safety(self, session_id: UUID) -> bool:
        row = await self._conn.fetchrow(
            "update sessions set status = 'paused_safety' where id = $1 and status = 'in_progress'"
            " returning id",
            session_id,
        )
        return row is not None
