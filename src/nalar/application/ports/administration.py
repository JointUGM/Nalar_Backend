from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from uuid import UUID

type AdminRow = dict[str, Any]


@dataclass(frozen=True)
class AdminPage:
    items: list[AdminRow]
    next_cursor: UUID | None
    total: int
    counts: dict[str, int] | None = None


@dataclass(frozen=True)
class ClassDetails:
    name: str
    grade_level: int
    academic_year_id: UUID
    homeroom_teacher_id: UUID | None = None


@dataclass(frozen=True)
class NewAcademicYear:
    name: str
    starts_on: date
    ends_on: date
    copy_classes_from: UUID | None = None


@dataclass(frozen=True)
class NewSchool:
    name: str
    npsn: str
    city: str
    admin_email: str


@dataclass(frozen=True)
class LearningOutcome:
    description: str
    element: str | None = None
    ordinal: int = 0


@dataclass(frozen=True)
class CurriculumSubject:
    name: str
    phase: str
    learning_outcomes: list[LearningOutcome]


@dataclass(frozen=True)
class NewCurriculum:
    name: str
    decree_code: str
    effective_on: date
    subjects: list[CurriculumSubject]
    is_current: bool = True


class AdministrationRepo(Protocol):
    """School and platform metadata; callers must authorize before accessing the repository."""

    async def lock_school(self, school_id: UUID) -> bool: ...

    async def kb_school(self, kb_id: UUID) -> UUID | None: ...

    async def people(
        self, school_id: UUID, role: str | None, q: str, cursor: UUID | None, limit: int
    ) -> AdminPage: ...

    async def edit_person(
        self, school_id: UUID, user_id: UUID, full_name: str | None, class_id: UUID | None
    ) -> None: ...

    async def deactivate_person(self, school_id: UUID, user_id: UUID) -> bool: ...

    async def classes(self, school_id: UUID, year_id: UUID | None) -> list[AdminRow]: ...

    async def save_class(
        self, school_id: UUID, details: ClassDetails, class_id: UUID | None
    ) -> UUID: ...

    async def subjects(self, school_id: UUID) -> list[AdminRow]: ...

    async def set_curriculum(
        self, school_id: UUID, subject_id: UUID, version_id: UUID, cp_subject_id: UUID | None
    ) -> None: ...

    async def assignments(self, school_id: UUID, year_id: UUID | None) -> list[AdminRow]: ...

    async def assign_teacher(
        self, school_id: UUID, class_id: UUID, subject_id: UUID, teacher_id: UUID | None
    ) -> None: ...

    async def transfer_kb(self, school_id: UUID, kb_id: UUID, teacher_id: UUID) -> None: ...

    async def create_year(self, school_id: UUID, details: NewAcademicYear) -> UUID: ...

    async def request(
        self, actor_id: UUID, operation: str, scope_id: UUID, key: UUID, digest: str
    ) -> tuple[UUID, UUID | None]: ...

    async def finish_request(self, request_id: UUID, result_id: UUID) -> None: ...

    async def schools(self, q: str, cursor: UUID | None, limit: int) -> AdminPage: ...

    async def create_school(self, actor_id: UUID, details: NewSchool) -> UUID: ...

    async def set_school_status(self, school_id: UUID, active: bool) -> bool: ...

    async def install_admin(self, school_id: UUID, user_id: UUID) -> None: ...

    async def pending_admin(self, school_id: UUID) -> UUID | None: ...

    async def lock_admin_handoffs(self, user_id: UUID) -> None: ...

    async def complete_admin_handoffs(self, user_id: UUID) -> None: ...

    async def curriculum_versions(self) -> list[AdminRow]: ...

    async def curriculum_version(self, version_id: UUID) -> AdminRow | None: ...

    async def publish_curriculum(self, details: NewCurriculum) -> UUID: ...
