import json
from datetime import datetime
from typing import cast
from uuid import UUID

import asyncpg

from nalar.application.errors import Conflict, InvalidInput, NotFound
from nalar.application.ports.administration import (
    AdminPage,
    AdminRow,
    ClassDetails,
    NewAcademicYear,
    NewCurriculum,
    NewPerson,
    NewSchool,
)
from nalar.infrastructure.db.pool import DbConnection

_PEOPLE = """
    with members as (
        select user_id, role::text as role, status = 'active' as active
          from school_memberships where school_id = $1
        union all
        select l.parent_id, 'parent', bool_or(l.deactivated_at is null and exists(
            select 1 from school_memberships m where m.school_id = l.school_id
              and m.user_id = l.student_id and m.role = 'student' and m.status = 'active'))
          from parent_student_links l where l.school_id = $1 group by l.parent_id
    ), people as (
        select p.id as user_id, p.full_name, p.contact_email, p.has_real_email,
               sp.nisn, array_agg(distinct m.role order by m.role) as roles,
               coalesce($2::text, (array_agg(m.role order by case m.role
                   when 'school_admin' then 0 when 'teacher' then 1
                   when 'student' then 2 else 3 end))[1]) as role,
               case when not bool_or(m.active) then 'inactive'
                    when p.onboarding_required then 'pending_activation'
                    else 'active' end as account_state
          from members m join profiles p on p.id = m.user_id
          left join student_profiles sp on sp.user_id = p.id
         group by p.id, sp.nisn
        having ($2::text is null or bool_or(m.role = $2))
           and ($3 = '' or position(lower($3) in lower(p.full_name || ' ' ||
                coalesce(p.contact_email, '') || ' ' || coalesce(sp.nisn, ''))) > 0)
    )
    select people.*, (select count(*)::int from people) as total,
           (select c.name from class_enrollments ce join classes c on c.id = ce.class_id
               join academic_years y on y.id = ce.academic_year_id
             where ce.school_id = $1 and ce.student_id = people.user_id and ce.status = 'active'
             order by y.is_current desc, y.starts_on desc, c.id limit 1) as class_name,
               coalesce((select jsonb_agg(jsonb_build_object(
                 'user_id', pr.id, 'full_name', pr.full_name))
             from parent_student_links l join profiles pr on pr.id = l.parent_id
             where l.school_id = $1 and l.student_id = people.user_id
               and l.deactivated_at is null), '[]') as linked_parents,
               coalesce((select jsonb_agg(jsonb_build_object(
                 'user_id', pr.id, 'full_name', pr.full_name))
             from parent_student_links l join profiles pr on pr.id = l.student_id
             where l.school_id = $1 and l.parent_id = people.user_id
               and l.deactivated_at is null), '[]') as linked_children
      from people where ($4::uuid is null or user_id > $4) order by user_id limit $5
"""

_CLASSES = """
    select c.id as class_id, c.name, c.grade_level, c.academic_year_id,
           c.homeroom_teacher_id, c.archived_at,
           (select count(*)::int from class_enrollments ce join school_memberships m
             on m.user_id = ce.student_id and m.school_id = ce.school_id
              and m.role = 'student' and m.status = 'active'
             where ce.class_id = c.id and ce.status = 'active') as student_count
      from classes c where c.school_id = $1 and c.archived_at is null
       and ($2::uuid is null or c.academic_year_id = $2) order by c.name, c.id
"""


class PgAdministrationRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def lock_school(self, school_id: UUID) -> bool:
        return (
            await self._conn.fetchval("select id from schools where id = $1 for update", school_id)
            is not None
        )

    async def kb_school(self, kb_id: UUID) -> UUID | None:
        school_id: UUID | None = await self._conn.fetchval(
            "select school_id from knowledge_bases where id = $1", kb_id
        )
        return school_id

    async def _students(
        self, school_id: UUID, user_ids: list[UUID], *, active: bool = True
    ) -> None:
        rows = await self._conn.fetch(
            "select user_id from school_memberships where school_id = $1"
            " and user_id = any($2::uuid[]) and role = 'student'"
            " and (not $3::bool or status = 'active')",
            school_id,
            user_ids,
            active,
        )
        if {row["user_id"] for row in rows} != set(user_ids):
            raise NotFound()

    async def validate_person(self, school_id: UUID, details: NewPerson) -> None:
        if details.class_id and not await self._conn.fetchval(
            "select exists(select 1 from classes where school_id = $1 and id = $2"
            " and archived_at is null)",
            school_id,
            details.class_id,
        ):
            raise NotFound()
        await self._students(school_id, details.child_ids)

    async def place_students(self, school_id: UUID, class_id: UUID, user_ids: list[UUID]) -> None:
        await self._students(school_id, user_ids)
        for user_id in sorted(set(user_ids)):
            await self.edit_person(school_id, user_id, None, class_id)

    async def reactivate_person(self, school_id: UUID, user_id: UUID) -> bool:
        await self._person(school_id, user_id)
        if await self._conn.fetchval(
            "select exists(select 1 from school_memberships where school_id = $1 and user_id = $2"
            " and role = 'school_admin' and status = 'inactive')",
            school_id,
            user_id,
        ):
            raise Conflict("ADMIN_REACTIVATION_REQUIRES_HANDOFF")
        snapshot = await self._conn.fetchrow(
            "select created_at, changes from audit_logs where school_id = $1 and entity_id = $2"
            " and action = 'admin.deactivate_person' order by created_at desc, id desc limit 1",
            school_id,
            user_id,
        )
        if snapshot is None:
            if await self._conn.fetchval(
                "select exists(select 1 from school_memberships where school_id = $1"
                " and user_id = $2 and status = 'inactive')",
                school_id,
                user_id,
            ):
                raise Conflict("REACTIVATION_HISTORY_MISSING")
            return False
        deactivated = snapshot["created_at"]
        snapshot_data = json.loads(snapshot["changes"])

        def ids(field: str) -> list[UUID] | None:
            return (
                [UUID(value) for value in snapshot_data[field]] if field in snapshot_data else None
            )

        changed = await self._conn.fetch(
            "update school_memberships set status = 'active', deactivated_at = null"
            " where school_id = $1 and user_id = $2 and status = 'inactive'"
            " and deactivated_at = $3 and ($4::uuid[] is null or id = any($4)) returning id",
            school_id,
            user_id,
            deactivated,
            ids("membership_ids"),
        )
        links = await self._conn.fetch(
            "update parent_student_links l set deactivated_at = null"
            " where school_id = $1 and parent_id = $2 and deactivated_at = $3"
            " and exists(select 1 from school_memberships m where m.school_id = $1"
            " and m.user_id = l.student_id and m.role = 'student' and m.status = 'active')"
            " and ($4::uuid[] is null or l.id = any($4)) returning id",
            school_id,
            user_id,
            deactivated,
            ids("parent_link_ids"),
        )
        await self._conn.execute(
            "update class_enrollments ce set status = 'active', ended_at = null"
            " where ce.school_id = $1 and ce.student_id = $2 and ce.ended_at = $3"
            " and ($4::uuid[] is null or ce.id = any($4))"
            " and ce.status = 'inactive' and exists(select 1 from classes c"
            " where c.id = ce.class_id and c.archived_at is null)"
            " and exists(select 1 from school_memberships m where m.school_id = $1"
            " and m.user_id = $2 and m.role = 'student' and m.status = 'active')"
            " and not exists(select 1 from class_enrollments active where active.student_id = $2"
            " and active.academic_year_id = ce.academic_year_id and active.status = 'active')",
            school_id,
            user_id,
            deactivated,
            ids("enrollment_ids"),
        )
        if changed or links:
            await self._conn.execute(
                "update profiles set credential_revision = credential_revision + 1 where id = $1",
                user_id,
            )
        return bool(changed or links)

    async def set_parent_link(
        self,
        school_id: UUID,
        parent_id: UUID,
        student_id: UUID,
        linked: bool,
        relationship: str | None,
    ) -> None:
        await self._person(school_id, parent_id)
        await self._students(school_id, [student_id], active=linked)
        if not await self._conn.fetchval(
            "select exists(select 1 from parent_student_links where school_id = $1"
            " and parent_id = $2)",
            school_id,
            parent_id,
        ):
            raise NotFound()
        if linked:
            await self.attach_parent(school_id, parent_id, student_id, relationship, restore=True)
        else:
            await self._conn.execute(
                "update parent_student_links set deactivated_at = now() where school_id = $1"
                " and parent_id = $2 and student_id = $3 and deactivated_at is null",
                school_id,
                parent_id,
                student_id,
            )

    async def attach_parent(
        self,
        school_id: UUID,
        parent_id: UUID,
        student_id: UUID,
        relationship: str | None,
        *,
        restore: bool,
    ) -> None:
        row = await self._conn.fetchval(
            "insert into parent_student_links(parent_id, student_id, school_id, relationship)"
            " values($2,$3,$1,$4) on conflict(parent_id, student_id) do update"
            " set relationship = excluded.relationship, deactivated_at = null"
            " where parent_student_links.school_id = excluded.school_id"
            " and ($5::bool or parent_student_links.deactivated_at is null) returning id",
            school_id,
            parent_id,
            student_id,
            relationship,
            restore,
        )
        if row is None:
            raise NotFound()

    async def roster_imports(
        self, school_id: UUID, year_id: UUID | None, cursor: UUID | None, limit: int
    ) -> AdminPage:
        if year_id:
            await self._year(school_id, year_id)
        after = None
        if cursor:
            after = await self._conn.fetchval(
                "select created_at from roster_imports where school_id = $1 and id = $2"
                " and ($3::uuid is null or academic_year_id = $3)",
                school_id,
                cursor,
                year_id,
            )
            if after is None:
                raise NotFound()
        rows = await self._conn.fetch(
            "select id as import_id, academic_year_id, status::text, rows_total,"
            " rows_succeeded, rows_failed, created_at, completed_at from roster_imports"
            " where school_id = $1 and ($2::uuid is null or academic_year_id = $2)"
            " and ($3::timestamptz is null or (created_at, id) < ($3, $4::uuid))"
            " order by created_at desc, id desc limit $5",
            school_id,
            year_id,
            after,
            cursor,
            limit + 1,
        )
        total = await self._conn.fetchval(
            "select count(*)::int from roster_imports where school_id = $1"
            " and ($2::uuid is null or academic_year_id = $2)",
            school_id,
            year_id,
        )
        items = [dict(row) for row in rows[:limit]]
        return AdminPage(items, items[-1]["import_id"] if len(rows) > limit else None, total)

    async def _teacher(self, school_id: UUID, teacher_id: UUID) -> None:
        if not await self._conn.fetchval(
            "select exists(select 1 from school_memberships where school_id = $1"
            " and user_id = $2 and role = 'teacher' and status = 'active')",
            school_id,
            teacher_id,
        ):
            raise NotFound()

    async def _year(self, school_id: UUID, year_id: UUID) -> None:
        if not await self._conn.fetchval(
            "select exists(select 1 from academic_years where school_id = $1 and id = $2)",
            school_id,
            year_id,
        ):
            raise NotFound()

    async def _person(self, school_id: UUID, user_id: UUID) -> None:
        if not await self._conn.fetchval(
            "select exists(select 1 from school_memberships where school_id = $1 and user_id = $2)"
            " or exists(select 1 from parent_student_links where school_id = $1 and"
            " parent_id = $2)",
            school_id,
            user_id,
        ):
            raise NotFound()

    async def people(
        self, school_id: UUID, role: str | None, q: str, cursor: UUID | None, limit: int
    ) -> AdminPage:
        rows = await self._conn.fetch(_PEOPLE, school_id, role, q, cursor, limit + 1)
        items = []
        for row in rows[:limit]:
            data = dict(row)
            data.pop("total")
            email = data.pop("contact_email")
            real = data.pop("has_real_email")
            local, _, domain = (email or "").partition("@")
            data["email"] = f"{local[:1]}***@{domain}" if real and domain else None
            data["linked_parents"] = json.loads(data["linked_parents"])
            data["linked_children"] = json.loads(data["linked_children"])
            items.append(data)
        total = (
            rows[0]["total"]
            if rows
            else await self._conn.fetchval(
                "select count(distinct m.user_id)::int from ("
                " select user_id, role::text as role from school_memberships where school_id = $1"
                " union all select parent_id, 'parent' from parent_student_links where"
                " school_id = $1"
                ") m join profiles p on p.id = m.user_id"
                " left join student_profiles sp on sp.user_id = p.id"
                " where ($2::text is null or m.role = $2) and ($3 = '' or position(lower($3)"
                " in lower(p.full_name || ' ' || coalesce(p.contact_email, '') || ' '"
                " || coalesce(sp.nisn, ''))) > 0)",
                school_id,
                role,
                q,
            )
        )
        return AdminPage(items, items[-1]["user_id"] if len(rows) > limit else None, total)

    async def edit_person(
        self, school_id: UUID, user_id: UUID, full_name: str | None, class_id: UUID | None
    ) -> None:
        await self._person(school_id, user_id)
        if class_id is not None:
            row = await self._conn.fetchrow(
                "select c.academic_year_id from classes c where c.id = $2 and c.school_id = $1"
                " and c.archived_at is null and exists(select 1 from school_memberships m"
                " where m.school_id = $1 and m.user_id = $3"
                " and m.role = 'student' and m.status = 'active')",
                school_id,
                class_id,
                user_id,
            )
            if row is None:
                raise NotFound()
            await self._conn.execute(
                "update class_enrollments set status = 'inactive', ended_at = now()"
                " where school_id = $1 and student_id = $2 and academic_year_id = $3"
                " and status = 'active' and class_id <> $4",
                school_id,
                user_id,
                row["academic_year_id"],
                class_id,
            )
            await self._conn.execute(
                "insert into class_enrollments(school_id, student_id, academic_year_id, class_id)"
                " values($1, $2, $3, $4) on conflict(student_id, academic_year_id)"
                " where status = 'active' do nothing",
                school_id,
                user_id,
                row["academic_year_id"],
                class_id,
            )
            placed = await self._conn.fetchval(
                "select class_id from class_enrollments where student_id = $1"
                " and academic_year_id = $2 and status = 'active'",
                user_id,
                row["academic_year_id"],
            )
            if placed != class_id:
                raise Conflict("CLASS_PLACEMENT_CONFLICT")
        if full_name is not None:
            await self._conn.execute(
                "update profiles set full_name = $2 where id = $1", user_id, full_name
            )

    async def deactivate_person(self, school_id: UUID, user_id: UUID) -> AdminRow | None:
        await self._person(school_id, user_id)
        is_admin = await self._conn.fetchval(
            "select exists(select 1 from school_memberships where school_id = $1"
            " and user_id = $2 and role = 'school_admin' and status = 'active')",
            school_id,
            user_id,
        )
        if is_admin and not await self._conn.fetchval(
            "select exists(select 1 from school_memberships m join profiles p on p.id = m.user_id"
            " where m.school_id = $1 and m.user_id <> $2 and m.role = 'school_admin'"
            " and m.status = 'active' and not p.onboarding_required)",
            school_id,
            user_id,
        ):
            raise Conflict("LAST_SCHOOL_ADMIN")
        changed = await self._conn.fetch(
            "update school_memberships set status = 'inactive', deactivated_at = now()"
            " where school_id = $1 and user_id = $2 and status = 'active' returning id",
            school_id,
            user_id,
        )
        links = await self._conn.fetch(
            "update parent_student_links set deactivated_at = now() where school_id = $1"
            " and parent_id = $2 and deactivated_at is null returning id",
            school_id,
            user_id,
        )
        enrollments = await self._conn.fetch(
            "update class_enrollments set status = 'inactive', ended_at = now()"
            " where school_id = $1 and student_id = $2 and status = 'active' returning id",
            school_id,
            user_id,
        )
        if changed or links:
            await self._conn.execute(
                "update schools set pending_admin_id = null"
                " where id = $1 and pending_admin_id = $2",
                school_id,
                user_id,
            )
            await self._conn.execute(
                "update profiles set credential_revision = credential_revision + 1 where id = $1",
                user_id,
            )
            return {
                "membership_ids": [str(row["id"]) for row in changed],
                "enrollment_ids": [str(row["id"]) for row in enrollments],
                "parent_link_ids": [str(row["id"]) for row in links],
            }
        return None

    async def classes(self, school_id: UUID, year_id: UUID | None) -> list[AdminRow]:
        if year_id:
            await self._year(school_id, year_id)
        return [dict(r) for r in await self._conn.fetch(_CLASSES, school_id, year_id)]

    async def save_class(
        self, school_id: UUID, details: ClassDetails, class_id: UUID | None
    ) -> UUID:
        await self._year(school_id, details.academic_year_id)
        if details.homeroom_teacher_id:
            await self._teacher(school_id, details.homeroom_teacher_id)
        try:
            if class_id is None:
                result: UUID = await self._conn.fetchval(
                    "insert into classes(school_id, academic_year_id, name, grade_level,"
                    " homeroom_teacher_id) values($1,$2,$3,$4,$5) returning id",
                    school_id,
                    details.academic_year_id,
                    details.name,
                    details.grade_level,
                    details.homeroom_teacher_id,
                )
            else:
                year = await self._conn.fetchval(
                    "select academic_year_id from classes where school_id = $1 and id = $2"
                    " and archived_at is null for update",
                    school_id,
                    class_id,
                )
                if year is None:
                    raise NotFound()
                if year != details.academic_year_id:
                    raise Conflict("CLASS_YEAR_IMMUTABLE")
                await self._conn.execute(
                    "update classes set name = $3, grade_level = $4, homeroom_teacher_id = $5"
                    " where school_id = $1 and id = $2",
                    school_id,
                    class_id,
                    details.name,
                    details.grade_level,
                    details.homeroom_teacher_id,
                )
                result = class_id
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("CLASS_NAME_EXISTS") from exc
        return result

    async def subjects(self, school_id: UUID) -> list[AdminRow]:
        rows = await self._conn.fetch(
            "select ss.id as school_subject_id, ss.name, cs.cp_version_id, ss.cp_subject_id,"
            " (select pr.full_name from knowledge_bases kb join profiles pr"
            " on pr.id = kb.owner_teacher_id where kb.school_subject_id = ss.id"
            " order by kb.created_at desc, kb.id limit 1) as kb_owner_name,"
            " coalesce((select jsonb_agg(jsonb_build_object('knowledge_base_id',kb.id,"
            " 'topic_title',kb.topic_title,'owner_teacher_id',kb.owner_teacher_id,"
            " 'owner_name',pr.full_name,'status',kb.status::text) order by kb.topic_title,kb.id)"
            " from knowledge_bases kb left join profiles pr on pr.id = kb.owner_teacher_id"
            " where kb.school_id = ss.school_id and kb.school_subject_id = ss.id), '[]')"
            " as knowledge_bases"
            " from school_subjects ss left join cp_subjects cs on cs.id = ss.cp_subject_id"
            " where ss.school_id = $1 order by ss.name, ss.id",
            school_id,
        )
        return [dict(r) | {"knowledge_bases": json.loads(r["knowledge_bases"])} for r in rows]

    async def create_subject(
        self, school_id: UUID, name: str, version_id: UUID, cp_subject_id: UUID
    ) -> UUID:
        published = await self._conn.fetchval(
            "select status = 'published' from cp_versions where id = $1", version_id
        )
        owner = await self._conn.fetchval(
            "select cp_version_id from cp_subjects where id = $1", cp_subject_id
        )
        if not published or owner is None:
            raise NotFound()
        if owner != version_id:
            raise InvalidInput("CURRICULUM_SUBJECT_REQUIRED")
        if await self._conn.fetchval(
            "select 1 from school_subjects where school_id = $1 and lower(name) = lower($2)",
            school_id,
            name,
        ):
            raise Conflict("SUBJECT_ALREADY_EXISTS")
        try:
            created: UUID = await self._conn.fetchval(
                "insert into school_subjects(school_id,name,cp_subject_id) values($1,$2,$3)"
                " returning id",
                school_id,
                name,
                cp_subject_id,
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("SUBJECT_ALREADY_EXISTS") from exc
        return created

    async def delete_subject(self, school_id: UUID, subject_id: UUID) -> str:
        try:
            name: str | None = await self._conn.fetchval(
                "delete from school_subjects where school_id = $1 and id = $2 returning name",
                school_id,
                subject_id,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise Conflict("SUBJECT_IN_USE") from exc
        if name is None:
            raise NotFound()
        return name

    async def published_cp_subject(self, version_id: UUID, cp_subject_id: UUID) -> AdminRow | None:
        row = await self._conn.fetchrow(
            "select cs.id,cs.name,cs.phase,cs.cp_version_id as version_id,"
            " coalesce((select jsonb_agg(jsonb_build_object('id',o.id,'element',o.element,"
            "'description',o.description,'ordinal',o.ordinal) order by o.ordinal,o.id)"
            " from cp_learning_outcomes o where o.cp_subject_id = cs.id),"
            " '[]') as learning_outcomes"
            " from cp_subjects cs join cp_versions v on v.id = cs.cp_version_id"
            " where cs.id = $2 and v.id = $1 and v.status = 'published'",
            version_id,
            cp_subject_id,
        )
        if row is None:
            return None
        return dict(row) | {"learning_outcomes": json.loads(row["learning_outcomes"])}

    async def set_curriculum(
        self, school_id: UUID, subject_id: UUID, version_id: UUID, cp_subject_id: UUID | None
    ) -> None:
        old = await self._conn.fetchrow(
            "select ss.id, coalesce(cs.name,ss.name) as name, cs.phase from school_subjects ss"
            " left join cp_subjects cs on cs.id = ss.cp_subject_id"
            " where ss.school_id = $1 and ss.id = $2",
            school_id,
            subject_id,
        )
        if old is None:
            raise NotFound()
        options = await self._conn.fetch(
            "select cs.id, cs.name, cs.phase from cp_subjects cs"
            " join cp_versions v on v.id = cs.cp_version_id"
            " where v.id = $1 and v.status = 'published' and"
            " (($2::uuid is not null and cs.id = $2) or ($2::uuid is null and cs.name = $3"
            " and ($4::text is null or cs.phase = $4)))",
            version_id,
            cp_subject_id,
            old["name"],
            old["phase"],
        )
        if not options:
            if cp_subject_id:
                raise NotFound()
            raise InvalidInput("CURRICULUM_SUBJECT_REQUIRED")
        if len(options) != 1:
            raise InvalidInput("CURRICULUM_SUBJECT_REQUIRED")
        new = options[0]
        if old["phase"] and old["phase"] != new["phase"]:
            raise InvalidInput("CURRICULUM_PHASE_MISMATCH")
        await self._conn.execute(
            "update school_subjects set cp_subject_id = $3 where school_id = $1 and id = $2",
            school_id,
            subject_id,
            new["id"],
        )

    async def assignments(self, school_id: UUID, year_id: UUID | None) -> list[AdminRow]:
        if year_id:
            await self._year(school_id, year_id)
        return [
            dict(r)
            for r in await self._conn.fetch(
                "select ta.class_id, c.name as class_name, c.academic_year_id,"
                " ta.school_subject_id,"
                " ss.name as subject_name, ta.teacher_id, p.full_name as teacher_name"
                " from teaching_assignments ta join classes c on c.id = ta.class_id"
                " join school_subjects ss on ss.id = ta.school_subject_id"
                " join profiles p on p.id = ta.teacher_id where ta.school_id = $1"
                " and c.archived_at is null and ($2::uuid is null or c.academic_year_id = $2)"
                " order by c.name, ss.name, p.full_name, ta.id",
                school_id,
                year_id,
            )
        ]

    async def assign_teacher(
        self, school_id: UUID, class_id: UUID, subject_id: UUID, teacher_id: UUID | None
    ) -> None:
        if not await self._conn.fetchval(
            "select exists(select 1 from classes c join school_subjects ss on ss.school_id"
            " = c.school_id"
            " where c.school_id = $1 and c.id = $2 and ss.id = $3 and c.archived_at is null)",
            school_id,
            class_id,
            subject_id,
        ):
            raise NotFound()
        if teacher_id:
            await self._teacher(school_id, teacher_id)
        await self._conn.execute(
            "delete from teaching_assignments where school_id = $1 and class_id = $2"
            " and school_subject_id = $3",
            school_id,
            class_id,
            subject_id,
        )
        if teacher_id:
            await self._conn.execute(
                "insert into teaching_assignments(school_id,class_id,school_subject_id,teacher_id)"
                " values($1,$2,$3,$4)",
                school_id,
                class_id,
                subject_id,
                teacher_id,
            )

    async def transfer_kb(self, school_id: UUID, kb_id: UUID, teacher_id: UUID) -> None:
        await self._teacher(school_id, teacher_id)
        row = await self._conn.fetchval(
            "update knowledge_bases kb set owner_teacher_id = $3 where kb.school_id = $1"
            " and kb.id = $2"
            " and exists(select 1 from teaching_assignments ta"
            " join classes c on c.id = ta.class_id and c.archived_at is null"
            " where ta.school_id = $1"
            " and ta.school_subject_id = kb.school_subject_id and ta.teacher_id = $3)"
            " returning kb.id",
            school_id,
            kb_id,
            teacher_id,
        )
        if row is None:
            raise NotFound()

    async def create_year(self, school_id: UUID, details: NewAcademicYear) -> UUID:
        if details.copy_classes_from:
            await self._year(school_id, details.copy_classes_from)
        try:
            await self._conn.execute(
                "update academic_years set is_current = false where school_id = $1 and is_current",
                school_id,
            )
            result: UUID = await self._conn.fetchval(
                "insert into academic_years(school_id,label,starts_on,ends_on,is_current)"
                " values($1,$2,$3,$4,true) returning id",
                school_id,
                details.name,
                details.starts_on,
                details.ends_on,
            )
            if details.copy_classes_from:
                await self._conn.execute(
                    "insert into classes(school_id,academic_year_id,name,grade_level)"
                    " select school_id,$3,name,grade_level from classes"
                    " where school_id = $1 and academic_year_id = $2 and archived_at is null",
                    school_id,
                    details.copy_classes_from,
                    result,
                )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("ACADEMIC_YEAR_EXISTS") from exc
        return result

    async def request(
        self, actor_id: UUID, operation: str, scope_id: UUID, key: UUID, digest: str
    ) -> tuple[UUID, UUID | None]:
        await self._conn.execute(
            "insert into admin_requests(actor_id,operation,scope_id,request_key,request_digest)"
            " values($1,$2,$3,$4,$5) on conflict do nothing",
            actor_id,
            operation,
            scope_id,
            key,
            digest,
        )
        row = await self._conn.fetchrow(
            "select id,result_id,request_digest from admin_requests where actor_id = $1"
            " and operation = $2 and scope_id = $3 and request_key = $4 for update",
            actor_id,
            operation,
            scope_id,
            key,
        )
        assert row is not None
        if row["request_digest"] != digest:
            raise Conflict("IDEMPOTENCY_CONFLICT")
        return row["id"], row["result_id"]

    async def request_receipt(self, request_id: UUID) -> AdminRow | None:
        value = await self._conn.fetchval(
            "select response_payload from admin_requests where id = $1", request_id
        )
        return cast(AdminRow, json.loads(value)) if value else None

    async def finish_receipt(self, request_id: UUID, result_id: UUID, receipt: AdminRow) -> None:
        await self._conn.execute(
            "update admin_requests set result_id = $2, response_payload = $3::jsonb"
            " where id = $1 and response_payload is null",
            request_id,
            result_id,
            json.dumps(receipt, default=str),
        )

    async def finish_request(self, request_id: UUID, result_id: UUID) -> None:
        await self._conn.execute(
            "update admin_requests set result_id = $2 where id = $1 and result_id is null",
            request_id,
            result_id,
        )

    async def schools(self, q: str, cursor: UUID | None, limit: int) -> AdminPage:
        counts = dict(
            await self._conn.fetchrow(
                "select count(*)::int as total, count(*) filter(where is_active)::int as active,"
                " count(*) filter(where not is_active)::int as suspended from schools"
            )
            or {}
        )
        total: int = await self._conn.fetchval(
            "select count(*)::int from schools where $1 = '' or position(lower($1)"
            " in lower(name || ' ' || coalesce(npsn,'') || ' ' || coalesce(city,''))) > 0",
            q,
        )
        rows = await self._conn.fetch(
            "select s.id,s.name,s.npsn,s.city,case when s.is_active then 'active' else 'suspended'"
            " end as status, (select p.full_name from school_memberships m join profiles p"
            " on p.id = m.user_id where m.school_id = s.id and m.role = 'school_admin'"
            " and m.status = 'active' order by m.joined_at,m.id limit 1) as admin_name,"
            " (select count(distinct user_id)::int from (select user_id from school_memberships"
            " where school_id = s.id and status = 'active' union select parent_id"
            " from parent_student_links where school_id = s.id and deactivated_at is null) u)"
            " as user_count from schools s where ($2::uuid is null or s.id > $2) and ($1 = ''"
            " or position(lower($1) in lower(s.name || ' ' || coalesce(s.npsn,'') || ' '"
            " || coalesce(s.city,''))) > 0) order by s.id limit $3",
            q,
            cursor,
            limit + 1,
        )
        items = [dict(r) for r in rows[:limit]]
        return AdminPage(items, items[-1]["id"] if len(rows) > limit else None, total, counts)

    async def create_school(self, actor_id: UUID, details: NewSchool) -> UUID:
        try:
            result: UUID = await self._conn.fetchval(
                "insert into schools(name,npsn,city,onboarded_by) values($1,$2,$3,$4) returning id",
                details.name,
                details.npsn,
                details.city,
                actor_id,
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("NPSN_EXISTS") from exc
        return result

    async def set_school_status(self, school_id: UUID, active: bool) -> bool:
        row = await self._conn.fetchval(
            "update schools set is_active = $2 where id = $1 and is_active <> $2 returning id",
            school_id,
            active,
        )
        return row is not None

    async def install_admin(self, school_id: UUID, user_id: UUID) -> None:
        previous = await self.pending_admin(school_id)
        if previous and previous != user_id:
            await self._conn.execute(
                "update school_memberships set status = 'inactive', deactivated_at = now()"
                " where school_id = $1 and user_id = $2 and role = 'school_admin'",
                school_id,
                previous,
            )
        await self._conn.execute(
            "insert into school_memberships(school_id,user_id,role,status)"
            " values($1,$2,'school_admin','active') on conflict(school_id,user_id,role)"
            " do update set status = 'active', deactivated_at = null",
            school_id,
            user_id,
        )
        await self._conn.execute(
            "update schools set pending_admin_id = $2 where id = $1", school_id, user_id
        )
        if await self._conn.fetchval(
            "select not onboarding_required from profiles where id = $1", user_id
        ):
            await self._conn.execute(
                "update schools set pending_admin_id = null where id = $1", school_id
            )
            await self._retire_admins(school_id, user_id)

    async def pending_admin(self, school_id: UUID) -> UUID | None:
        result: UUID | None = await self._conn.fetchval(
            "select pending_admin_id from schools where id = $1", school_id
        )
        return result

    async def lock_admin_handoffs(self, user_id: UUID) -> None:
        await self._conn.fetch(
            "select id from schools where pending_admin_id = $1 order by id for update", user_id
        )

    async def complete_admin_handoffs(self, user_id: UUID) -> None:
        completed = await self._conn.fetch(
            "update schools sc set pending_admin_id = null where sc.pending_admin_id = $1"
            " and exists(select 1 from profiles p where p.id = $1 and not p.onboarding_required)"
            " and exists(select 1 from school_memberships m where m.school_id = sc.id"
            " and m.user_id = $1 and m.role = 'school_admin' and m.status = 'active')"
            " returning sc.id",
            user_id,
        )
        for row in completed:
            await self._retire_admins(row["id"], user_id)

    async def _retire_admins(self, school_id: UUID, user_id: UUID) -> None:
        old = await self._conn.fetch(
            "update school_memberships set status = 'inactive',deactivated_at = now()"
            " where school_id = $1 and role = 'school_admin' and user_id <> $2"
            " and status = 'active' returning user_id",
            school_id,
            user_id,
        )
        if old:
            await self._conn.execute(
                "update profiles set credential_revision = credential_revision + 1"
                " where id = any($1::uuid[])",
                [r["user_id"] for r in old],
            )

    async def curriculum_versions(self) -> list[AdminRow]:
        return [
            dict(r)
            for r in await self._conn.fetch(
                "select v.id,v.title as name,v.decree_code,v.effective_on,v.published_at,"
                " v.status::text as status,v.is_current,"
                " (select count(distinct ss.school_id)::int from school_subjects ss join"
                " cp_subjects cs"
                " on cs.id = ss.cp_subject_id where cs.cp_version_id = v.id) as school_count"
                " from cp_versions v order by v.effective_on desc,v.id"
            )
        ]

    async def published_curriculum_versions(self) -> list[AdminRow]:
        rows = await self._conn.fetch(
            "select v.id,v.title as name,v.decree_code,v.effective_on,v.published_at,v.is_current,"
            " coalesce((select jsonb_agg(jsonb_build_object("
            " 'id',cs.id,'name',cs.name,'phase',cs.phase)"
            " order by cs.name,cs.phase,cs.id) from cp_subjects cs where cs.cp_version_id = v.id),"
            " '[]') as subjects from cp_versions v where v.status = 'published'"
            " order by v.is_current desc,v.effective_on desc,v.id"
        )
        return [dict(r) | {"subjects": json.loads(r["subjects"])} for r in rows]

    async def curriculum_version(self, version_id: UUID) -> AdminRow | None:
        versions = await self.curriculum_versions()
        version = next((v for v in versions if v["id"] == version_id), None)
        if version is None:
            return None
        rows = await self._conn.fetch(
            "select cs.id,cs.name,cs.phase,coalesce((select jsonb_agg(jsonb_build_object("
            " 'id',o.id,'element',o.element,'description',o.description,'ordinal',o.ordinal)"
            " order by o.ordinal,o.id) from cp_learning_outcomes o where o.cp_subject_id = cs.id),"
            " '[]') as learning_outcomes from cp_subjects cs where cs.cp_version_id = $1"
            " order by cs.name,cs.phase,cs.id",
            version_id,
        )
        version["subjects"] = [
            dict(r) | {"learning_outcomes": json.loads(r["learning_outcomes"])} for r in rows
        ]
        return version

    async def publish_curriculum(self, details: NewCurriculum) -> UUID:
        await self._conn.execute("select pg_advisory_xact_lock(hashtextextended('cp-current',0))")
        try:
            if details.is_current:
                await self._conn.execute(
                    "update cp_versions set is_current = false where is_current"
                )
            result: UUID = await self._conn.fetchval(
                "insert into"
                " cp_versions(title,decree_code,effective_on,status,published_at,is_current)"
                " values($1,$2,$3,'published',now(),$4) returning id",
                details.name,
                details.decree_code,
                details.effective_on,
                details.is_current,
            )
            for subject in details.subjects:
                subject_id: UUID = await self._conn.fetchval(
                    "insert into cp_subjects(cp_version_id,name,phase) values($1,$2,$3) returning"
                    " id",
                    result,
                    subject.name,
                    subject.phase,
                )
                await self._conn.executemany(
                    "insert into cp_learning_outcomes(cp_subject_id,element,description,ordinal)"
                    " values($1,$2,$3,$4)",
                    [
                        (subject_id, o.element, o.description, o.ordinal)
                        for o in subject.learning_outcomes
                    ],
                )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("CURRICULUM_EXISTS") from exc
        return result

    async def school_detail(self, school_id: UUID) -> AdminRow | None:
        row = await self._conn.fetchrow(
            "select s.id,s.name,s.npsn,s.city,case when s.is_active then 'active' else "
            "'suspended' end as status,"
            " (select p.full_name from school_memberships m join profiles p on p.id = m.user_id"
            " where m.school_id = s.id and m.role = 'school_admin' and m.status = 'active'"
            " order by m.joined_at,m.id limit 1) as admin_name,"
            " (select count(distinct user_id)::int from (select user_id from school_memberships"
            " where school_id = s.id and status = 'active' union select parent_id from "
            "parent_student_links"
            " where school_id = s.id and deactivated_at is null) users) as user_count "
            "from schools s where s.id = $1",
            school_id,
        )
        return dict(row) if row else None

    async def edit_school(self, school_id: UUID, fields: AdminRow) -> None:
        try:
            row = await self._conn.fetchval(
                "update schools set name = coalesce($2,name), npsn = coalesce($3,npsn),"
                " city = coalesce($4,city) where id = $1 returning id",
                school_id,
                fields.get("name"),
                fields.get("npsn"),
                fields.get("city"),
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("NPSN_EXISTS") from exc
        if row is None:
            raise NotFound()

    async def ai_usage(self, start: datetime, end: datetime) -> list[AdminRow]:
        rows = await self._conn.fetch(
            "select (created_at at time zone 'UTC')::date as day, school_id, purpose::text, model,"
            " count(*)::int as calls, count(*) filter(where status = 'failed')::int as "
            "failed_calls,"
            " coalesce(sum(input_tokens),0)::bigint as input_tokens,"
            " coalesce(sum(output_tokens),0)::bigint as output_tokens,"
            " coalesce(sum(cost_usd),0)::double precision as cost_usd from ai_invocations"
            " where created_at >= $1 and created_at < $2 group by day,school_id,purpose,model"
            " order by day desc,school_id,purpose,model",
            start,
            end,
        )
        return [dict(row) for row in rows]

    async def audit_page(
        self, school_id: UUID | None, limit: int, before: int | None
    ) -> list[AdminRow]:
        rows = await self._conn.fetch(
            "select id,school_id,actor_id,action,entity_table,entity_id,created_at from audit_logs"
            " where ($1::uuid is null or school_id = $1) and ($3::bigint is null or id < $3)"
            " order by id desc limit $2",
            school_id,
            limit,
            before,
        )
        return [dict(row) for row in rows]
