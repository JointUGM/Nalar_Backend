from uuid import UUID

from nalar.infrastructure.db.pool import DbConnection

# The {school} and {publication} slots take only fixed SQL fragments from this file;
# every request value arrives as a $n parameter.
_ACTIVE_TEACHER = """
    exists (select 1 from school_memberships m
             where m.school_id = {school} and m.user_id = $1
               and m.role = 'teacher' and m.status = 'active'
               and exists(select 1 from schools sc where sc.id = m.school_id and sc.is_active))
"""

_TEACHES_PUBLICATION = f"""
    select exists (
        select 1
          from publications p
          join classes c on c.id = p.class_id and c.archived_at is null
          join mission_versions mv on mv.id = p.mission_version_id
          join missions mi on mi.id = mv.mission_id
          join knowledge_bases kb on kb.id = mi.knowledge_base_id
          join teaching_assignments ta
            on ta.class_id = p.class_id and ta.school_subject_id = kb.school_subject_id
         where p.id = ({{publication}}) and ta.teacher_id = $1
           and {_ACTIVE_TEACHER.format(school="p.school_id")})
"""


class PgAuthz:
    async def is_platform_admin(self, user_id: UUID) -> bool:
        return await self._check(
            "select exists(select 1 from profiles where id = $1 and is_platform_admin)", user_id
        )

    async def manages_kb(self, user_id: UUID, kb_id: UUID) -> bool:
        return await self._check(
            "select exists(select 1 from knowledge_bases kb join schools sc on sc.id = kb.school_id"
            " join school_memberships m on m.school_id = sc.id"
            " where kb.id = $2 and sc.is_active and m.user_id = $1"
            " and m.role = 'school_admin' and m.status = 'active'"
            " and exists(select 1 from schools sc where sc.id = m.school_id and sc.is_active))",
            user_id,
            kb_id,
        )

    async def can_manage_roster(self, user_id: UUID, import_id: UUID) -> bool:
        return bool(
            await self._conn.fetchval(
                "select exists(select 1 from roster_imports r join school_memberships m"
                " on m.school_id = r.school_id where r.id = $2 and m.user_id = $1"
                " and m.role = 'school_admin' and m.status = 'active'"
                " and exists(select 1 from schools sc where sc.id = m.school_id and sc.is_active))",
                user_id,
                import_id,
            )
        )

    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def _check(self, sql: str, *args: object) -> bool:
        return bool(await self._conn.fetchval(sql, *args))

    async def teaches_class(self, user_id: UUID, class_id: UUID) -> bool:
        return await self._check(
            f"""select exists (
                  select 1 from teaching_assignments ta
                    join classes c on c.id = ta.class_id and c.archived_at is null
                   where ta.teacher_id = $1 and ta.class_id = $2
                     and {_ACTIVE_TEACHER.format(school="ta.school_id")})""",
            user_id,
            class_id,
        )

    async def teaches_class_subject(
        self, user_id: UUID, class_id: UUID, school_subject_id: UUID
    ) -> bool:
        return await self._check(
            f"""select exists (
                  select 1 from teaching_assignments ta
                    join classes c on c.id = ta.class_id and c.archived_at is null
                   where ta.teacher_id = $1 and ta.class_id = $2 and ta.school_subject_id = $3
                     and {_ACTIVE_TEACHER.format(school="ta.school_id")})""",
            user_id,
            class_id,
            school_subject_id,
        )

    async def teaches_subject(
        self, user_id: UUID, school_id: UUID, school_subject_id: UUID
    ) -> bool:
        return await self._check(
            f"""select exists (
                  select 1 from teaching_assignments ta
                    join classes c on c.id = ta.class_id and c.archived_at is null
                   where ta.teacher_id = $1 and ta.school_id = $2 and ta.school_subject_id = $3
                     and {_ACTIVE_TEACHER.format(school="ta.school_id")})""",
            user_id,
            school_id,
            school_subject_id,
        )

    async def teaches_publication(self, user_id: UUID, publication_id: UUID) -> bool:
        return await self._check(
            _TEACHES_PUBLICATION.format(publication="$2"), user_id, publication_id
        )

    async def teaches_run(self, user_id: UUID, run_id: UUID) -> bool:
        return await self._check(
            _TEACHES_PUBLICATION.format(
                publication="select publication_id from publication_runs where id = $2"
            ),
            user_id,
            run_id,
        )

    async def teaches_session(self, user_id: UUID, session_id: UUID) -> bool:
        return await self._check(
            _TEACHES_PUBLICATION.format(
                publication="select publication_id from sessions where id = $2"
            ),
            user_id,
            session_id,
        )

    async def can_read_kb(self, user_id: UUID, kb_id: UUID) -> bool:
        return await self._check(
            f"""select exists (
                  select 1 from knowledge_bases kb
                   where kb.id = $2 and {_ACTIVE_TEACHER.format(school="kb.school_id")}
                     and (kb.owner_teacher_id = $1
                          or exists (select 1 from teaching_assignments ta
                                      where ta.school_id = kb.school_id
                                        and ta.school_subject_id = kb.school_subject_id
                                        and ta.teacher_id = $1)))""",
            user_id,
            kb_id,
        )

    async def owns_kb(self, user_id: UUID, kb_id: UUID) -> bool:
        return await self._check(
            f"""select exists (
                  select 1 from knowledge_bases kb
                   where kb.id = $2 and kb.owner_teacher_id = $1
                     and {_ACTIVE_TEACHER.format(school="kb.school_id")})""",
            user_id,
            kb_id,
        )

    async def is_mission_creator(self, user_id: UUID, mission_id: UUID) -> bool:
        return await self._check(
            f"""select exists (
                  select 1 from missions mi
                   where mi.id = $2 and mi.created_by = $1
                     and {_ACTIVE_TEACHER.format(school="mi.school_id")})""",
            user_id,
            mission_id,
        )

    async def is_enrolled(self, user_id: UUID, class_id: UUID) -> bool:
        return await self._check(
            """select exists (
                 select 1 from class_enrollments ce
                   join school_memberships m
                     on m.school_id = ce.school_id and m.user_id = ce.student_id
                    and m.role = 'student' and m.status = 'active'
                    and exists(select 1 from schools sc
                                where sc.id = m.school_id and sc.is_active)
                  where ce.student_id = $1 and ce.class_id = $2 and ce.status = 'active')""",
            user_id,
            class_id,
        )

    async def owns_session(self, user_id: UUID, session_id: UUID) -> bool:
        return await self._check(
            """select exists (
                 select 1 from sessions s
                   join school_memberships m
                     on m.school_id = s.school_id and m.user_id = s.student_id
                    and m.role = 'student' and m.status = 'active'
                    and exists(select 1 from schools sc where sc.id = m.school_id and sc.is_active)
                  where s.id = $2 and s.student_id = $1)""",
            user_id,
            session_id,
        )

    async def is_linked_parent(self, user_id: UUID, student_id: UUID) -> bool:
        return await self._check(
            "select exists(select 1 from parent_student_links l"
            " join schools sc on sc.id = l.school_id"
            " join school_memberships m on m.school_id = l.school_id and m.user_id = l.student_id"
            " where l.parent_id = $1 and l.student_id = $2 and l.deactivated_at is null"
            " and sc.is_active and m.role = 'student' and m.status = 'active')",
            user_id,
            student_id,
        )

    async def is_school_admin(self, user_id: UUID, school_id: UUID) -> bool:
        return await self._check(
            "select exists (select 1 from school_memberships where school_id = $2"
            " and exists(select 1 from schools sc where sc.id = $2 and sc.is_active)"
            " and user_id = $1 and role = 'school_admin' and status = 'active')",
            user_id,
            school_id,
        )

    async def is_school_teacher(self, user_id: UUID, school_id: UUID) -> bool:
        return await self._check(
            "select exists (select 1 from school_memberships where school_id = $2"
            " and exists(select 1 from schools sc where sc.id = $2 and sc.is_active)"
            " and user_id = $1 and role = 'teacher' and status = 'active')",
            user_id,
            school_id,
        )

    async def is_school_student(self, user_id: UUID, school_id: UUID) -> bool:
        return await self._check(
            "select exists (select 1 from school_memberships where school_id = $2"
            " and exists(select 1 from schools sc where sc.id = $2 and sc.is_active)"
            " and user_id = $1 and role = 'student' and status = 'active')",
            user_id,
            school_id,
        )

    async def teaches_student(self, user_id: UUID, student_id: UUID) -> bool:
        return await self._check(
            f"""select exists(select 1 from class_enrollments ce
            join teaching_assignments ta on ta.class_id = ce.class_id
            where ce.student_id = $2 and ta.teacher_id = $1
            and {_ACTIVE_TEACHER.format(school="ta.school_id")})""",
            user_id,
            student_id,
        )
