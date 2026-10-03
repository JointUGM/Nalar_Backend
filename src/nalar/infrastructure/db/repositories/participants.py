import json
from datetime import datetime
from uuid import UUID

import asyncpg

from nalar.application.ports.participants import JoinTarget, Participant
from nalar.domain.labels import ParticipantStatus, RunMode, RunStatus
from nalar.infrastructure.db.pool import DbConnection

_TARGET = """
    select r.id as run_id, r.school_id, r.publication_id, p.class_id, r.mode::text as mode,
           r.status::text as status, mi.title as mission_title, mv.anchor_problem,
           mv.max_duration_minutes, mv.live_warmup, r.kind::text as kind,
           r.grant_student_id, r.opens_at, r.closes_at
      from publication_runs r
      join publications p on p.id = r.publication_id and p.cancelled_at is null
      join mission_versions mv on mv.id = p.mission_version_id
      join missions mi on mi.id = mv.mission_id
"""
_PARTICIPANT = (
    "select id, run_id, student_id, status::text as status, warmup_choice_id,"
    " warmup_submitted_at, session_id from run_participants"
)


def _target(row: asyncpg.Record | None) -> JoinTarget | None:
    if row is None:
        return None
    data = dict(row)
    warmup = data.pop("live_warmup")
    return JoinTarget(
        **{**data, "mode": RunMode(data["mode"]), "status": RunStatus(data["status"])},
        live_warmup=json.loads(warmup) if warmup else None,
    )


def _participant(row: asyncpg.Record | None) -> Participant | None:
    if row is None:
        return None
    data = dict(row)
    return Participant(**{**data, "status": ParticipantStatus(data["status"])})


class PgParticipantsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def joinable_by_code(self, code: str) -> JoinTarget | None:
        return _target(
            await self._conn.fetchrow(
                _TARGET + " where r.join_code = $1 and r.kind = 'primary'"
                " and r.status in ('lobby', 'open')"
                " for share of r",
                code,
            )
        )

    async def latest_closed_by_code(self, code: str) -> JoinTarget | None:
        return _target(
            await self._conn.fetchrow(
                _TARGET + " where r.join_code = $1 and r.kind = 'primary' and r.status = 'closed'"
                " order by r.closed_at desc limit 1",
                code,
            )
        )

    async def run_target(self, run_id: UUID) -> JoinTarget | None:
        return _target(await self._conn.fetchrow(_TARGET + " where r.id = $1", run_id))

    async def window_target(
        self, publication_id: UUID, run_id: UUID | None = None, lock: bool = False
    ) -> JoinTarget | None:
        return _target(
            await self._conn.fetchrow(
                _TARGET + " where r.publication_id = $1"
                " and (($2::uuid is null and r.kind = 'primary') or r.id = $2)"
                + (" for share of r" if lock else ""),
                publication_id,
                run_id,
            )
        )

    async def get(self, run_id: UUID, student_id: UUID) -> Participant | None:
        return _participant(
            await self._conn.fetchrow(
                _PARTICIPANT + " where run_id = $1 and student_id = $2", run_id, student_id
            )
        )

    async def insert_waiting(
        self, school_id: UUID, run_id: UUID, student_id: UUID, now: datetime
    ) -> Participant:
        await self._conn.execute(
            "insert into run_participants (school_id, run_id, student_id, joined_at)"
            " values ($1, $2, $3, $4) on conflict (run_id, student_id) do nothing",
            school_id,
            run_id,
            student_id,
            now,
        )
        participant = await self.get(run_id, student_id)
        assert participant is not None
        return participant

    async def insert_started(
        self, school_id: UUID, run_id: UUID, student_id: UUID, session_id: UUID, now: datetime
    ) -> Participant:
        await self._conn.execute(
            "insert into run_participants"
            " (school_id, run_id, student_id, status, session_id, joined_at)"
            " values ($1, $2, $3, 'started', $4, $5) on conflict (run_id, student_id) do nothing",
            school_id,
            run_id,
            student_id,
            session_id,
            now,
        )
        participant = await self.get(run_id, student_id)
        assert participant is not None
        return participant

    async def submit_warmup(self, participant_id: UUID, choice_id: str, now: datetime) -> bool:
        row = await self._conn.fetchrow(
            "update run_participants set warmup_choice_id = $2, warmup_submitted_at = $3"
            " where id = $1 and warmup_choice_id is null and status = 'waiting' returning id",
            participant_id,
            choice_id,
            now,
        )
        return row is not None
