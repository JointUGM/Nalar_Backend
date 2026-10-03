from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from nalar.application.ports.roster import AcademicYear, ImportRef, ImportView
from nalar.domain.roster import RowError
from nalar.infrastructure.db.pool import DbConnection


class PgRosterRepo:
    async def academic_years(self, school_id: UUID) -> list[AcademicYear]:
        rows = await self._conn.fetch(
            "select id, label as name, starts_on, ends_on, is_current from academic_years"
            " where school_id = $1 order by is_current desc, starts_on desc, id",
            school_id,
        )
        return [AcademicYear(**dict(row)) for row in rows]

    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def claim(
        self, import_id: UUID, job_id: UUID, now: datetime, stale_before: datetime
    ) -> int | None:
        status = await self._conn.fetchval(
            "select status::text from roster_imports where id = $1 for update",
            import_id,
        )
        if status in ("completed", "failed"):
            return 0
        attempt: int | None = await self._conn.fetchval(
            "update jobs set status = 'running', attempts = attempts + 1, updated_at = $3"
            " where id = $2 and entity_id = $1 and entity_type = 'roster_imports'"
            " and kind = 'roster_import'"
            " and (status = 'queued' or (status = 'running' and updated_at <= $4))"
            " returning attempts",
            import_id,
            job_id,
            now,
            stale_before,
        )
        return attempt

    async def keepalive(self, import_id: UUID, job_id: UUID, attempt: int, now: datetime) -> bool:
        await self._conn.fetchval(
            "select id from roster_imports where id = $1 for update", import_id
        )
        return (
            await self._conn.fetchval(
                "update jobs set updated_at = $4 where id = $2 and entity_id = $1"
                " and status = 'running' and attempts = $3"
                " returning id",
                import_id,
                job_id,
                attempt,
                now,
            )
            is not None
        )

    async def release_claim(self, import_id: UUID, job_id: UUID, attempt: int) -> None:
        await self._conn.fetchval(
            "select id from roster_imports where id = $1 for update", import_id
        )
        updated = await self._conn.fetchval(
            "update jobs set status = 'queued' where id = $2 and entity_id = $1"
            " and status = 'running' and attempts = $3 returning id",
            import_id,
            job_id,
            attempt,
        )
        if updated:
            await self._conn.execute(
                "update roster_imports set status = 'pending' where id = $1", import_id
            )

    async def fail(self, import_id: UUID) -> None:
        await self._conn.execute(
            "update roster_imports set status = 'failed'"
            " where id = $1 and status in ('pending', 'processing')",
            import_id,
        )

    async def year_in_school(self, year_id: UUID, school_id: UUID) -> bool:
        return bool(
            await self._conn.fetchval(
                "select exists (select 1 from academic_years where id = $1 and school_id = $2)",
                year_id,
                school_id,
            )
        )

    async def create_import(
        self, import_id: UUID, school_id: UUID, year_id: UUID, uploader_id: UUID, path: str
    ) -> None:
        await self._conn.execute(
            "insert into roster_imports"
            " (id, school_id, academic_year_id, uploaded_by, storage_path)"
            " values ($1, $2, $3, $4, $5)",
            import_id,
            school_id,
            year_id,
            uploader_id,
            path,
        )

    async def import_ref(self, import_id: UUID) -> ImportRef | None:
        row = await self._conn.fetchrow(
            "select id, school_id, academic_year_id, storage_bucket, storage_path, uploaded_by"
            "  from roster_imports where id = $1",
            import_id,
        )
        return ImportRef(**dict(row)) if row else None

    async def start(self, import_id: UUID) -> None:
        await self._conn.execute("delete from roster_import_errors where import_id = $1", import_id)
        await self._conn.execute(
            "update roster_imports set status = 'processing' where id = $1", import_id
        )

    async def student_by_nisn(self, nisn: str) -> UUID | None:
        user_id: UUID | None = await self._conn.fetchval(
            "select user_id from student_profiles where nisn = $1", nisn
        )
        return user_id

    async def profile_by_email(self, email: str) -> UUID | None:
        user_id: UUID | None = await self._conn.fetchval(
            "select id from profiles where lower(contact_email) = lower($1)"
            " order by created_at limit 1",
            email,
        )
        return user_id

    async def ensure_profile(
        self,
        user_id: UUID,
        full_name: str,
        email: str | None,
        has_real_email: bool,
        *,
        onboarding_required: bool = False,
    ) -> bool:
        inserted = await self._conn.fetchval(
            "insert into profiles"
            " (id, full_name, contact_email, has_real_email, onboarding_required)"
            " values ($1, $2, $3, $4, $5) on conflict (id) do nothing returning id",
            user_id,
            full_name,
            email,
            has_real_email,
            onboarding_required,
        )
        return inserted is not None

    async def claim_nisn(self, user_id: UUID, nisn: str) -> bool:
        await self._conn.execute(
            "insert into student_profiles (user_id, nisn) values ($1, $2) on conflict do nothing",
            user_id,
            nisn,
        )
        return await self.student_by_nisn(nisn) == user_id

    async def ensure_membership(self, school_id: UUID, user_id: UUID, role: str) -> bool:
        await self._conn.execute(
            "insert into school_memberships (school_id, user_id, role)"
            " values ($1, $2, $3::membership_role)"
            " on conflict (school_id, user_id, role) do nothing",
            school_id,
            user_id,
            role,
        )
        return bool(
            await self._conn.fetchval(
                "select exists(select 1 from school_memberships where school_id = $1"
                " and user_id = $2 and role = $3::membership_role and status = 'active')",
                school_id,
                user_id,
                role,
            )
        )

    async def ensure_class(
        self, school_id: UUID, year_id: UUID, name: str, grade_level: int
    ) -> UUID | None:
        class_id: UUID | None = await self._conn.fetchval(
            "insert into classes (school_id, academic_year_id, name, grade_level)"
            " values ($1, $2, $3, $4)"
            " on conflict (academic_year_id, name) do update set name = excluded.name"
            " where classes.grade_level = excluded.grade_level"
            " and classes.archived_at is null returning id",
            school_id,
            year_id,
            name,
            grade_level,
        )
        return class_id

    async def ensure_enrollment(
        self, school_id: UUID, year_id: UUID, class_id: UUID, student_id: UUID
    ) -> None:
        await self._conn.execute(
            "insert into class_enrollments (school_id, academic_year_id, class_id, student_id)"
            " select $1, $2, $3, $4 where not exists (select 1 from class_enrollments"
            "   where student_id = $4 and academic_year_id = $2 and status = 'active')"
            " on conflict (student_id, academic_year_id) where status = 'active' do nothing",
            school_id,
            year_id,
            class_id,
            student_id,
        )

    async def ensure_parent_link(
        self, parent_id: UUID, student_id: UUID, school_id: UUID, relationship: str | None
    ) -> None:
        await self._conn.execute(
            "insert into parent_student_links (parent_id, student_id, school_id, relationship)"
            " values ($1, $2, $3, $4) on conflict (parent_id, student_id) do nothing",
            parent_id,
            student_id,
            school_id,
            relationship,
        )

    async def finish(
        self, import_id: UUID, total: int, errors: Sequence[RowError], now: datetime
    ) -> None:
        await self._conn.executemany(
            "insert into roster_import_errors (import_id, row_number, field, message)"
            " values ($1, $2, $3, $4)",
            [(import_id, e.row_number, e.field, e.message) for e in errors],
        )
        failed = len({e.row_number for e in errors if e.row_number > 1})
        await self._conn.execute(
            "update roster_imports set status = $5::import_status, rows_total = $2,"
            " rows_succeeded = $2::int - $3::int, rows_failed = $3, completed_at = $4"
            " where id = $1",
            import_id,
            total,
            failed,
            now,
            "failed" if any(e.row_number == 1 for e in errors) else "completed",
        )

    async def import_view(self, import_id: UUID) -> ImportView | None:
        head = await self._conn.fetchrow(
            "select status::text as status, rows_total, rows_succeeded, rows_failed"
            "  from roster_imports where id = $1",
            import_id,
        )
        if head is None:
            return None
        errors = await self._conn.fetch(
            "select row_number, field, message from roster_import_errors where import_id = $1"
            " order by row_number",
            import_id,
        )
        return ImportView(
            head["status"],
            head["rows_total"],
            head["rows_succeeded"],
            head["rows_failed"],
            tuple(RowError(e["row_number"], e["field"] or "", e["message"]) for e in errors),
        )
