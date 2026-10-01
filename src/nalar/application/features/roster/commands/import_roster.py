from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from nalar.application.features.roster.commands.upload_roster import RosterLimits
from nalar.application.features.roster.messages import ROSTER_KIND
from nalar.application.ports.auth_admin import AuthAdmin, AuthAdminError
from nalar.application.ports.clock import Clock
from nalar.application.ports.roster import ImportRef
from nalar.application.ports.storage import ObjectStorage
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.roster import RosterRow, RowError, parse_roster


class RowRejected(Exception):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message


class ImportInterrupted(Exception):
    pass


@dataclass(frozen=True)
class ImportClaim:
    ref: ImportRef
    job_id: UUID
    attempt: int


class ImportRosterHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        storage: ObjectStorage,
        admin: AuthAdmin,
        clock: Clock,
        limits: RosterLimits,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._admin = admin
        self._clock = clock
        self._limits = limits

    async def execute(self, import_id: UUID, job_id: UUID) -> None:
        async with self._uow:
            job = await self._uow.jobs.get(job_id)
            if (
                job is None
                or job.kind != ROSTER_KIND
                or job.entity_type != "roster_imports"
                or job.entity_id != import_id
            ):
                raise ImportInterrupted("Import job does not match")
            if job.status in ("succeeded", "failed"):
                return
            ref = await self._uow.roster.import_ref(import_id)
            if ref is None or ref.school_id != job.school_id or ref.uploaded_by != job.requested_by:
                raise ImportInterrupted("Import scope does not match")
            if not await self._uow.authz.is_school_admin(ref.uploaded_by, ref.school_id):
                await self._uow.roster.fail(import_id)
                await self._uow.jobs.mark_failed(job_id, "IMPORT_FORBIDDEN", "")
                return
            now = self._clock.now()
            attempt = await self._uow.roster.claim(
                import_id,
                job_id,
                now,
                now - timedelta(seconds=self._limits.stale_after_s),
            )
            if attempt == 0:
                return
            if attempt is None:
                raise ImportInterrupted("Import already has an active worker")
            await self._uow.roster.start(import_id)
        claim = ImportClaim(ref, job_id, attempt)
        try:
            rows: list[RosterRow]
            data = await self._storage.download(ref.storage_bucket, ref.storage_path)
            if len(data) > self._limits.max_bytes:
                rows, errors = [], [RowError(1, "file", "Berkas lebih dari batas ukuran.")]
            else:
                try:
                    rows, errors = parse_roster(data.decode("utf-8-sig"))
                except UnicodeDecodeError:
                    rows, errors = [], [RowError(1, "file", "Berkas harus UTF-8.")]
            total = len(rows) + sum(1 for e in errors if e.row_number > 1)
            for row in rows:
                try:
                    if row.role == "student":
                        await self._student(claim, row)
                    else:
                        await self._teacher(claim, row)
                except RowRejected as rejected:
                    errors.append(RowError(row.row_number, rejected.field, rejected.message))
                except AuthAdminError as exc:
                    if exc.retryable:
                        raise
                    errors.append(RowError(row.row_number, "email", "Akun tidak dapat dibuat."))
            async with self._uow:
                await self._guard(claim)
                await self._uow.roster.finish(import_id, total, errors, self._clock.now())
                if any(e.row_number == 1 for e in errors):
                    await self._uow.jobs.mark_failed(job_id, "INVALID_ROSTER", "")
                else:
                    await self._uow.jobs.mark_succeeded(job_id)
        except Exception:
            async with self._uow:
                await self._uow.roster.release_claim(import_id, job_id, attempt)
            raise

    async def _guard(self, claim: ImportClaim) -> None:
        ref = claim.ref
        if not await self._uow.authz.is_school_admin(ref.uploaded_by, ref.school_id):
            raise ImportInterrupted("Import authorization changed")
        if not await self._uow.roster.keepalive(
            ref.id, claim.job_id, claim.attempt, self._clock.now()
        ):
            raise ImportInterrupted("Import claim changed")

    async def _account(self, claim: ImportClaim, email: str, full_name: str, real: bool) -> UUID:
        async with self._uow:
            await self._guard(claim)
            existing = await self._uow.roster.profile_by_email(email) if real else None
        user_id = existing or await self._admin.create_or_find(email, full_name)
        async with self._uow:
            await self._guard(claim)
            await self._uow.roster.ensure_profile(user_id, full_name, email if real else None, real)
        return user_id

    async def _student(self, claim: ImportClaim, row: RosterRow) -> None:
        ref = claim.ref
        assert row.nisn and row.class_name and row.grade_level
        async with self._uow:
            await self._guard(claim)
            student_id = await self._uow.roster.student_by_nisn(row.nisn)
        if student_id is None:
            login = row.email or f"{row.nisn}@{self._limits.student_login_domain}"
            student_id = await self._account(
                claim, login, row.full_name, real=row.email is not None
            )
        parent_id = None
        if row.parent_email:
            parent_id = await self._account(
                claim,
                row.parent_email,
                row.parent_name or f"Orang tua {row.full_name}",
                real=True,
            )
        async with self._uow:
            await self._guard(claim)
            if not await self._uow.roster.claim_nisn(student_id, row.nisn):
                raise RowRejected("nisn", "NISN tidak dapat digunakan untuk akun ini.")
            if not await self._uow.roster.ensure_membership(ref.school_id, student_id, "student"):
                raise RowRejected("role", "Keanggotaan akun tidak aktif.")
            class_id = await self._uow.roster.ensure_class(
                ref.school_id,
                ref.academic_year_id,
                row.class_name,
                row.grade_level,
            )
            if class_id is None:
                raise RowRejected("class_name", "Kelas diarsipkan atau tingkat kelas berbeda.")
            await self._uow.roster.ensure_enrollment(
                ref.school_id,
                ref.academic_year_id,
                class_id,
                student_id,
            )
            if parent_id:
                await self._uow.roster.ensure_parent_link(
                    parent_id, student_id, ref.school_id, row.relationship
                )

    async def _teacher(self, claim: ImportClaim, row: RosterRow) -> None:
        assert row.email
        teacher_id = await self._account(claim, row.email, row.full_name, real=True)
        async with self._uow:
            await self._guard(claim)
            if not await self._uow.roster.ensure_membership(
                claim.ref.school_id, teacher_id, "teacher"
            ):
                raise RowRejected("role", "Keanggotaan akun tidak aktif.")
