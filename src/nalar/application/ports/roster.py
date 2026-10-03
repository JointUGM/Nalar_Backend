from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from nalar.domain.roster import RowError


@dataclass(frozen=True)
class AcademicYear:
    id: UUID
    name: str
    starts_on: date
    ends_on: date
    is_current: bool


@dataclass(frozen=True)
class ImportRef:
    id: UUID
    school_id: UUID
    academic_year_id: UUID
    storage_bucket: str
    storage_path: str
    uploaded_by: UUID


@dataclass(frozen=True)
class ImportView:
    status: str
    rows_total: int | None
    rows_succeeded: int | None
    rows_failed: int | None
    errors: tuple[RowError, ...]


class RosterRepo(Protocol):
    async def academic_years(self, school_id: UUID) -> list[AcademicYear]: ...

    async def claim(
        self, import_id: UUID, job_id: UUID, now: datetime, stale_before: datetime
    ) -> int | None:
        """Zero for terminal imports, None for an active lease, otherwise the fenced attempt."""
        ...

    async def keepalive(
        self, import_id: UUID, job_id: UUID, attempt: int, now: datetime
    ) -> bool: ...

    async def release_claim(self, import_id: UUID, job_id: UUID, attempt: int) -> None: ...

    async def fail(self, import_id: UUID) -> None: ...

    async def year_in_school(self, year_id: UUID, school_id: UUID) -> bool: ...

    async def create_import(
        self, import_id: UUID, school_id: UUID, year_id: UUID, uploader_id: UUID, path: str
    ) -> None: ...

    async def import_ref(self, import_id: UUID) -> ImportRef | None: ...

    async def start(self, import_id: UUID) -> None:
        """processing, with any errors of an interrupted earlier run cleared."""
        ...

    async def student_by_nisn(self, nisn: str) -> UUID | None: ...

    async def profile_by_email(self, email: str) -> UUID | None: ...

    async def ensure_profile(
        self,
        user_id: UUID,
        full_name: str,
        email: str | None,
        has_real_email: bool,
        *,
        onboarding_required: bool = False,
    ) -> bool:
        """Insert a profile and its onboarding marker atomically; report whether inserted."""
        ...

    async def claim_nisn(self, user_id: UUID, nisn: str) -> bool:
        """False when the NISN already belongs to another account."""
        ...

    async def ensure_membership(self, school_id: UUID, user_id: UUID, role: str) -> bool: ...

    async def ensure_class(
        self, school_id: UUID, year_id: UUID, name: str, grade_level: int
    ) -> UUID | None: ...

    async def ensure_enrollment(
        self, school_id: UUID, year_id: UUID, class_id: UUID, student_id: UUID
    ) -> None:
        """No-op when the student already has an active class this year."""
        ...

    async def ensure_parent_link(
        self, parent_id: UUID, student_id: UUID, school_id: UUID, relationship: str | None
    ) -> None: ...

    async def finish(
        self, import_id: UUID, total: int, errors: Sequence[RowError], now: datetime
    ) -> None: ...

    async def import_view(self, import_id: UUID) -> ImportView | None: ...
