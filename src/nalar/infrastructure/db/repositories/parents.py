from datetime import datetime
from uuid import UUID

from nalar.application.ports.parents import (
    Child,
    DigestItem,
    DigestRecipient,
    ParentReflection,
    VisibleResult,
)
from nalar.infrastructure.db.pool import DbConnection

# NFR-S3 / PA rules, the one visibility rule for parents (app and digest): the publication is
# released, the child's latest finished attempt is completed with a completed evaluation, and
# the child's summary is stored. Anything else leaves no trace, not even a count.
VISIBLE_SQL = """
    select p.id as publication_id, ls.id as session_id, mi.title as mission_title,
           p.released_to_parents_at as released_at, ls.ended_at as completed_at,
           ps.content as summary
      from parent_summaries ps
      join publications p on p.id = ps.publication_id
       and p.released_to_parents_at is not null and p.cancelled_at is null
       and exists(select 1 from schools sc where sc.id = p.school_id and sc.is_active)
       and exists(select 1 from school_memberships m where m.school_id = p.school_id
                   and m.user_id = $1 and m.role = 'student' and m.status = 'active')
      join v_latest_sessions ls on ls.publication_id = p.id and ls.student_id = ps.student_id
       and ls.status = 'completed'
      join session_evaluations e on e.session_id = ls.id and e.status = 'completed'
      join mission_versions mv on mv.id = p.mission_version_id
      join missions mi on mi.id = mv.mission_id
     where ps.student_id = $1
"""


class PgParentsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def digest_recipients(
        self,
        released_after: datetime | None,
        parent_id: UUID | None = None,
        *,
        for_submission: bool = False,
    ) -> list[DigestRecipient]:
        if for_submission:
            if parent_id is None:
                raise ValueError("submission requires a parent")
            await self._conn.fetch("select id from profiles where id = $1 for update", parent_id)
            await self._conn.fetch(
                "select student_id from parent_student_links where parent_id = $1"
                " order by student_id for update",
                parent_id,
            )
            await self._conn.fetch(
                "select p.id from publications p"
                " join parent_summaries ps on ps.publication_id = p.id"
                " join parent_student_links l on l.student_id = ps.student_id"
                " where l.parent_id = $1 order by p.id for share of p, ps",
                parent_id,
            )
            await self._conn.fetch(
                "select s.id from sessions s"
                " join parent_student_links l on l.student_id = s.student_id"
                " where l.parent_id = $1 order by s.id for share of s",
                parent_id,
            )
            await self._conn.fetch(
                "select e.id from session_evaluations e join sessions s on s.id = e.session_id"
                " join parent_student_links l on l.student_id = s.student_id"
                " where l.parent_id = $1 order by e.id for share of e",
                parent_id,
            )
        rows = await self._conn.fetch(
            f"""
            select pr.id as parent_id, l.school_id, pr.contact_email,
                   l.student_id, ch.full_name as child_name,
                   v.publication_id, v.session_id, v.mission_title, v.summary
              from parent_student_links l
              join profiles pr on pr.id = l.parent_id
              join profiles ch on ch.id = l.student_id
              join lateral ({VISIBLE_SQL.replace("$1", "l.student_id")}) v on true
             where l.deactivated_at is null and pr.weekly_digest_enabled and pr.has_real_email
               and nullif(trim(pr.contact_email), '') is not null
               and ($1::timestamptz is null or v.released_at >= $1)
               and ($2::uuid is null or pr.id = $2)
             order by pr.id, l.student_id, v.publication_id
            """,
            released_after,
            parent_id,
        )
        grouped: dict[UUID, list[DigestItem]] = {}
        recipients: dict[UUID, tuple[UUID, str]] = {}
        for row in rows:
            pid = row["parent_id"]
            recipients[pid] = (row["school_id"], row["contact_email"])
            grouped.setdefault(pid, []).append(
                DigestItem(
                    student_id=row["student_id"],
                    publication_id=row["publication_id"],
                    session_id=row["session_id"],
                    child_name=row["child_name"],
                    mission_title=row["mission_title"],
                    summary=row["summary"],
                )
            )
        return [
            DigestRecipient(pid, *recipients[pid], tuple(items)) for pid, items in grouped.items()
        ]

    async def children(self, parent_id: UUID, limit: int, after: UUID | None) -> list[Child]:
        rows = await self._conn.fetch(
            "select l.student_id, pr.full_name as name, sc.name as school_name, sc.id as school_id,"
            " current_class.name as class_name, seen.last_seen_at"
            "  from parent_student_links l"
            "  join profiles pr on pr.id = l.student_id"
            "  join schools sc on sc.id = l.school_id"
            " left join parent_child_seen seen on seen.parent_id = l.parent_id"
            " and seen.student_id = l.student_id"
            " left join lateral(select c.name from class_enrollments ce"
            " join classes c on c.id = ce.class_id join academic_years y on y.id = "
            "c.academic_year_id"
            " where ce.student_id = l.student_id and ce.school_id = l.school_id"
            " and ce.status = 'active' and c.archived_at is null and y.is_current"
            " order by c.id limit 1) current_class on true"
            " where l.parent_id = $1 and l.deactivated_at is null and sc.is_active"
            " and exists(select 1 from school_memberships m where m.user_id = l.student_id"
            " and m.school_id = l.school_id and m.role = 'student' and m.status = 'active')"
            " and ($3::uuid is null or l.student_id > $3)"
            " order by l.student_id limit $2",
            parent_id,
            limit,
            after,
        )
        return [Child(**dict(r)) for r in rows]

    async def visible_results(self, student_id: UUID) -> list[VisibleResult]:
        rows = await self._conn.fetch(
            f"{VISIBLE_SQL} order by p.released_to_parents_at desc", student_id
        )
        return [VisibleResult(**dict(r)) for r in rows]

    async def concepts_seen(self, student_id: UUID) -> list[tuple[str, str]]:
        rows = await self._conn.fetch(
            f"""
            with visible as ({VISIBLE_SQL})
            select distinct on (r.concept_id) c.name, r.outcome::text as outcome
              from visible v
              join session_concept_results r on r.session_id = v.session_id
              join concepts c on c.id = r.concept_id
             where r.outcome <> 'not_observed'
             order by r.concept_id, v.completed_at desc
            """,
            student_id,
        )
        return sorted((r["name"], r["outcome"]) for r in rows)

    async def reflections(
        self, student_id: UUID, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[ParentReflection]:
        rows = await self._conn.fetch(
            f"""
            with visible as ({VISIBLE_SQL})
            select v.session_id, v.mission_title, v.completed_at, sr.content,
                   ss.name as subject_name
              from visible v join session_reflections sr on sr.session_id = v.session_id
              join publications p on p.id = v.publication_id
              join mission_versions mv on mv.id = p.mission_version_id
              join missions mi on mi.id = mv.mission_id
              join knowledge_bases kb on kb.id = mi.knowledge_base_id
              join school_subjects ss on ss.id = kb.school_subject_id
             where ($3::timestamptz is null or (v.completed_at, v.session_id) < ($3, $4))
             order by v.completed_at desc, v.session_id desc
             limit $2
            """,
            student_id,
            limit,
            after[0] if after else None,
            after[1] if after else None,
        )
        return [ParentReflection(**dict(r)) for r in rows]

    async def digest_enabled(self, user_id: UUID) -> bool:
        return bool(
            await self._conn.fetchval(
                "select weekly_digest_enabled from profiles where id = $1", user_id
            )
        )

    async def set_digest(self, user_id: UUID, enabled: bool) -> None:
        await self._conn.execute(
            "update profiles set weekly_digest_enabled = $2 where id = $1", user_id, enabled
        )

    async def mark_seen(self, parent_id: UUID, student_id: UUID, now: datetime) -> None:
        await self._conn.execute(
            "insert into parent_child_seen(parent_id, student_id, last_seen_at) values($1,$2,$3)"
            " on conflict(parent_id,student_id) do update"
            " set last_seen_at = greatest(parent_child_seen.last_seen_at, excluded.last_seen_at)",
            parent_id,
            student_id,
            now,
        )
