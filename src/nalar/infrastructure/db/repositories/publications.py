from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, InvalidInput, NotFound
from nalar.application.ports.administration import AdminRow
from nalar.application.ports.publications import (
    Assignment,
    ClassRef,
    GrantPublication,
    NewRun,
    PublicationCounts,
    PublicationSummary,
    Published,
    RunSummary,
    StoredGrant,
    VersionForPublish,
)
from nalar.domain.labels import RunMode, RunStatus
from nalar.infrastructure.db.pool import DbConnection

_TEACHES_PUBLICATION_CLASS = """
    exists (select 1 from teaching_assignments ta
              join school_memberships m on m.school_id = ta.school_id and m.user_id = ta.teacher_id
                                       and m.role = 'teacher' and m.status = 'active'
             where ta.teacher_id = $1 and ta.class_id = p.class_id
               and ta.school_subject_id = kb.school_subject_id)
"""


class PgPublicationsRepo:
    async def detail(self, publication_id: UUID) -> AdminRow | None:
        row = await self._conn.fetchrow(
            "select p.id, p.school_id, p.class_id, c.name as class_name, p.published_by,"
            " p.created_at, p.cancelled_at, p.released_to_parents_at,"
            " mv.id as mission_version_id, mv.version_number, mi.id as mission_id,"
            " mi.title as mission_title, mi.knowledge_base_id"
            " from publications p join classes c on c.id = p.class_id"
            " join mission_versions mv on mv.id = p.mission_version_id"
            " join missions mi on mi.id = mv.mission_id where p.id = $1",
            publication_id,
        )
        if row is None:
            return None
        runs = await self._conn.fetch(
            "select id, kind::text, mode::text, status::text, opens_at, closes_at,"
            " join_code from publication_runs where publication_id = $1"
            " order by kind, created_at, id",
            publication_id,
        )
        return {**dict(row), "runs": [dict(run) for run in runs]}

    async def edit(
        self,
        publication_id: UUID,
        now: datetime,
        opens_at: datetime | None,
        closes_at: datetime | None,
        *,
        cancel: bool,
    ) -> UUID:
        publication = await self.lock_for_grant(publication_id)
        if publication is None:
            raise NotFound()
        runs = await self._conn.fetch(
            "select id, kind::text, mode::text, status::text, opens_at, started_at"
            " from publication_runs where publication_id = $1 order by id for update",
            publication_id,
        )
        if cancel and publication.cancelled_at is not None:
            return publication.school_id
        if publication.cancelled_at or publication.released_to_parents_at:
            raise Conflict("PUBLICATION_NOT_EDITABLE")
        if await self._conn.fetchval(
            "select exists(select 1 from sessions where publication_id = $1)", publication_id
        ):
            raise Conflict("PUBLICATION_HAS_SESSIONS")
        if cancel:
            await self._conn.execute(
                "update publications set cancelled_at = $2 where id = $1 and cancelled_at is null",
                publication_id,
                now,
            )
            await self._conn.execute(
                "update run_participants set status = 'cancelled' where run_id = any($1::uuid[])"
                " and status = 'waiting'",
                [run["id"] for run in runs],
            )
            await self._conn.execute(
                "update publication_runs set status = 'closed', closed_at = $2"
                " where publication_id = $1 and status <> 'closed'",
                publication_id,
                now,
            )
        else:
            primary = next((run for run in runs if run["kind"] == "primary"), None)
            if primary is None:
                raise NotFound()
            if (
                primary["mode"] != "window"
                or primary["status"] != "scheduled"
                or primary["started_at"] is not None
                or primary["opens_at"] <= now
            ):
                raise Conflict("PUBLICATION_NOT_EDITABLE")
            if opens_at is None or closes_at is None or not now < opens_at < closes_at:
                raise InvalidInput("INVALID_WINDOW")
            changed = await self._conn.fetchval(
                "update publication_runs set opens_at = $2, closes_at = $3"
                " where id = $1 and status = 'scheduled' returning id",
                primary["id"],
                opens_at,
                closes_at,
            )
            if changed is None:
                raise Conflict("PUBLICATION_NOT_EDITABLE")
        return publication.school_id

    async def lock_for_grant(self, publication_id: UUID) -> GrantPublication | None:
        # Lifecycle writes must not block the foreign-key locks of a live session start.
        row = await self._conn.fetchrow(
            "select school_id, class_id, cancelled_at, released_to_parents_at"
            " from publications where id = $1 for no key update",
            publication_id,
        )
        return GrantPublication(**dict(row)) if row else None

    async def grant_by_key(
        self, publication_id: UUID, actor_id: UUID, key: UUID
    ) -> StoredGrant | None:
        row = await self._conn.fetchrow(
            "select id as run_id, grant_request_digest as digest from publication_runs"
            " where publication_id = $1 and granted_by = $2 and grant_request_key = $3",
            publication_id,
            actor_id,
            key,
        )
        return StoredGrant(**dict(row)) if row else None

    async def has_outstanding_grant(
        self, publication_id: UUID, student_id: UUID, now: datetime
    ) -> bool:
        await self._conn.execute(
            "update publication_runs set status = 'closed', closed_at = closes_at"
            " where publication_id = $1 and grant_student_id = $2 and kind = 'grant'"
            " and status in ('scheduled', 'open') and closes_at <= $3",
            publication_id,
            student_id,
            now,
        )
        return bool(
            await self._conn.fetchval(
                "select exists(select 1 from publication_runs where publication_id = $1"
                " and grant_student_id = $2 and kind = 'grant' and status <> 'closed')",
                publication_id,
                student_id,
            )
        )

    async def create_grant(
        self,
        publication_id: UUID,
        school_id: UUID,
        student_id: UUID,
        actor_id: UUID,
        key: UUID,
        digest: str,
        reason: str,
        opens_at: datetime,
        closes_at: datetime,
        now: datetime,
    ) -> UUID:
        run_id: UUID = await self._conn.fetchval(
            "insert into publication_runs (school_id, publication_id, kind, mode, status,"
            " grant_student_id, granted_by, grant_reason, grant_request_key, grant_request_digest,"
            " opens_at, closes_at) values ($1, $2, 'grant', 'window',"
            " case when $8::timestamptz <= $10::timestamptz"
            " then 'open'::run_status else 'scheduled'::run_status end,"
            " $3, $4, $5, $6, $7, $8, $9) returning id",
            school_id,
            publication_id,
            student_id,
            actor_id,
            reason,
            key,
            digest,
            opens_at,
            closes_at,
            now,
        )
        return run_id

    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def teacher_ids(self, publication_id: UUID) -> list[UUID]:
        rows = await self._conn.fetch(
            "select distinct ta.teacher_id from publications p"
            " join mission_versions mv on mv.id = p.mission_version_id"
            " join missions mi on mi.id = mv.mission_id"
            " join knowledge_bases kb on kb.id = mi.knowledge_base_id"
            " join teaching_assignments ta on ta.class_id = p.class_id"
            "  and ta.school_subject_id = kb.school_subject_id"
            " join school_memberships m on m.school_id = ta.school_id"
            "  and m.user_id = ta.teacher_id and m.role = 'teacher' and m.status = 'active'"
            " where p.id = $1",
            publication_id,
        )
        return [r["teacher_id"] for r in rows]

    async def version_for_publish(self, version_id: UUID) -> VersionForPublish | None:
        row = await self._conn.fetchrow(
            "select mv.id, mv.school_id, mv.created_by, kb.school_subject_id,"
            " mv.reviewed_at is not null as reviewed"
            " from mission_versions mv join missions mi on mi.id = mv.mission_id"
            " join knowledge_bases kb on kb.id = mi.knowledge_base_id"
            " where mv.id = $1 and mi.archived_at is null",
            version_id,
        )
        return VersionForPublish(**dict(row)) if row else None

    async def class_ref(self, class_id: UUID) -> ClassRef | None:
        row = await self._conn.fetchrow(
            "select id, school_id from classes where id = $1 and archived_at is null", class_id
        )
        return ClassRef(**dict(row)) if row else None

    async def lock_pair(self, version_id: UUID, class_id: UUID) -> None:
        await self._conn.execute(
            "select pg_advisory_xact_lock("
            "hashtextextended($1::uuid::text || ':' || $2::uuid::text, 0))",
            version_id,
            class_id,
        )

    async def find_active(self, version_id: UUID, class_id: UUID) -> Published | None:
        row = await self._conn.fetchrow(
            "select p.id, r.id as run_id, r.status::text as status from publications p"
            " join publication_runs r on r.publication_id = p.id and r.kind = 'primary'"
            " where p.mission_version_id = $1 and p.class_id = $2 and p.cancelled_at is null"
            " and r.status <> 'closed' order by p.created_at desc limit 1",
            version_id,
            class_id,
        )
        return Published(row["id"], row["run_id"], RunStatus(row["status"])) if row else None

    async def create(
        self,
        *,
        school_id: UUID,
        version_id: UUID,
        class_id: UUID,
        publisher_id: UUID,
        run: NewRun,
        planner_mode: str,
    ) -> Published:
        publication_id: UUID = await self._conn.fetchval(
            "insert into publications (school_id, mission_version_id, class_id, published_by)"
            " values ($1, $2, $3, $4) returning id",
            school_id,
            version_id,
            class_id,
            publisher_id,
        )
        run_id: UUID = await self._conn.fetchval(
            "insert into publication_runs (school_id, publication_id, kind, mode, status,"
            " opens_at, closes_at, planner_mode) values ($1, $2, 'primary', $3::run_mode,"
            " 'scheduled', $4, $5, $6::planner_mode) returning id",
            school_id,
            publication_id,
            run.mode.value,
            run.opens_at,
            run.closes_at,
            planner_mode,
        )
        return Published(publication_id, run_id, RunStatus.scheduled)

    async def teacher_assignments(self, teacher_id: UUID) -> list[Assignment]:
        rows = await self._conn.fetch(
            "select ta.school_id, c.id as class_id, c.name as class_name, c.grade_level,"
            " ss.id as school_subject_id, ss.name as subject_name"
            " from teaching_assignments ta"
            " join classes c on c.id = ta.class_id and c.archived_at is null"
            " join academic_years y on y.id = c.academic_year_id and y.is_current"
            " join school_subjects ss on ss.id = ta.school_subject_id"
            " join school_memberships m on m.school_id = ta.school_id and m.user_id = ta.teacher_id"
            "  and m.role = 'teacher' and m.status = 'active'"
            " where ta.teacher_id = $1 order by c.name, ss.name",
            teacher_id,
        )
        return [Assignment(**dict(r)) for r in rows]

    async def teacher_publications(
        self,
        teacher_id: UUID,
        class_id: UUID | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[PublicationSummary]:
        # The f-string slot takes only the fixed fragment above; values are $n parameters.
        rows = await self._conn.fetch(
            f"""select p.id, mi.title, c.id as class_id, c.name as class_name, p.created_at,
                       p.released_to_parents_at, r.id as run_id, r.mode::text as mode,
                       r.status::text as status, r.opens_at, r.closes_at, r.join_code,
                       (select count(*) from sessions s where s.publication_id = p.id) as started,
                       (select count(*) from sessions s where s.publication_id = p.id
                          and s.status = 'completed') as completed,
                       (select count(*) from sessions s where s.publication_id = p.id
                          and s.status = 'timed_out') as timed_out,
                       (select count(*) from session_evaluations e
                          join sessions s on s.id = e.session_id
                         where s.publication_id = p.id) as evaluated
                  from publications p
                  join publication_runs r on r.publication_id = p.id and r.kind = 'primary'
                  join classes c on c.id = p.class_id
                  join mission_versions mv on mv.id = p.mission_version_id
                  join missions mi on mi.id = mv.mission_id
                  join knowledge_bases kb on kb.id = mi.knowledge_base_id
                 where p.cancelled_at is null and {_TEACHES_PUBLICATION_CLASS}
                   and ($2::uuid is null or p.class_id = $2)
                   and ($3::timestamptz is null or (p.created_at, p.id) < ($3, $4::uuid))
                 order by p.created_at desc, p.id desc limit $5""",
            teacher_id,
            class_id,
            after[0] if after else None,
            after[1] if after else None,
            limit,
        )
        return [
            PublicationSummary(
                id=r["id"],
                mission_title=r["title"],
                class_id=r["class_id"],
                class_name=r["class_name"],
                created_at=r["created_at"],
                released_to_parents_at=r["released_to_parents_at"],
                run=RunSummary(
                    r["run_id"],
                    RunMode(r["mode"]),
                    RunStatus(r["status"]),
                    r["opens_at"],
                    r["closes_at"],
                    r["join_code"],
                ),
                counts=PublicationCounts(
                    r["started"], r["completed"], r["timed_out"], r["evaluated"]
                ),
            )
            for r in rows
        ]
