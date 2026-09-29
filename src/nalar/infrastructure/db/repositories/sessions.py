from datetime import datetime
from uuid import UUID

import asyncpg

from nalar.application.ports.sessions import SessionRef, StudentMissionRow
from nalar.domain.labels import RunMode, RunStatus, SessionStatus
from nalar.infrastructure.db.pool import DbConnection

_REF = "select id, run_id, status::text as status, started_at, deadline_at from sessions"

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
