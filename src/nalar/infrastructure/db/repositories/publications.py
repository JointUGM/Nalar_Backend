from datetime import datetime
from uuid import UUID

from nalar.application.ports.publications import (
    Assignment,
    ClassRef,
    NewRun,
    PublicationCounts,
    PublicationSummary,
    Published,
    RunSummary,
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
