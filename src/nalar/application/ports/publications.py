from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from nalar.domain.labels import RunMode, RunStatus


@dataclass(frozen=True)
class VersionForPublish:
    id: UUID
    school_id: UUID
    created_by: UUID
    school_subject_id: UUID
    reviewed: bool


@dataclass(frozen=True)
class ClassRef:
    id: UUID
    school_id: UUID


@dataclass(frozen=True)
class NewRun:
    mode: RunMode
    opens_at: datetime | None
    closes_at: datetime | None
    planner_mode: str | None


@dataclass(frozen=True)
class Published:
    publication_id: UUID
    run_id: UUID
    run_status: RunStatus


@dataclass(frozen=True)
class Assignment:
    school_id: UUID
    class_id: UUID
    class_name: str
    grade_level: int
    school_subject_id: UUID
    subject_name: str


@dataclass(frozen=True)
class RunSummary:
    id: UUID
    mode: RunMode
    status: RunStatus
    opens_at: datetime | None
    closes_at: datetime | None
    join_code: str | None


@dataclass(frozen=True)
class PublicationCounts:
    started: int
    completed: int
    timed_out: int
    evaluated: int


@dataclass(frozen=True)
class PublicationSummary:
    id: UUID
    mission_title: str
    class_id: UUID
    class_name: str
    created_at: datetime
    released_to_parents_at: datetime | None
    run: RunSummary
    counts: PublicationCounts


class PublicationsRepo(Protocol):
    async def version_for_publish(self, version_id: UUID) -> VersionForPublish | None: ...

    async def class_ref(self, class_id: UUID) -> ClassRef | None: ...

    async def lock_pair(self, version_id: UUID, class_id: UUID) -> None:
        """Serialize concurrent publishes of one version to one class until commit."""
        ...

    async def find_active(self, version_id: UUID, class_id: UUID) -> Published | None: ...

    async def create(
        self,
        *,
        school_id: UUID,
        version_id: UUID,
        class_id: UUID,
        publisher_id: UUID,
        run: NewRun,
        planner_mode: str,
    ) -> Published: ...

    async def teacher_assignments(self, teacher_id: UUID) -> list[Assignment]: ...

    async def teacher_publications(
        self,
        teacher_id: UUID,
        class_id: UUID | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[PublicationSummary]: ...
