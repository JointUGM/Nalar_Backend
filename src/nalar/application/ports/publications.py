from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from nalar.application.ports.administration import AdminRow
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
    subject_name: str = ""
    mission_id: UUID | None = None
    mission_version: int | None = None


@dataclass(frozen=True)
class GrantPublication:
    school_id: UUID
    class_id: UUID
    cancelled_at: datetime | None
    released_to_parents_at: datetime | None


@dataclass(frozen=True)
class StoredGrant:
    run_id: UUID
    digest: str


class PublicationsRepo(Protocol):
    async def detail(self, publication_id: UUID) -> AdminRow | None: ...

    async def edit(
        self,
        publication_id: UUID,
        now: datetime,
        opens_at: datetime | None,
        closes_at: datetime | None,
        *,
        cancel: bool,
    ) -> UUID: ...

    async def lock_for_grant(self, publication_id: UUID) -> GrantPublication | None: ...

    async def grant_by_key(
        self, publication_id: UUID, actor_id: UUID, key: UUID
    ) -> StoredGrant | None: ...

    async def has_outstanding_grant(
        self, publication_id: UUID, student_id: UUID, now: datetime
    ) -> bool: ...

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
    ) -> UUID: ...

    async def teacher_ids(self, publication_id: UUID) -> list[UUID]:
        """Active teachers assigned to the publication's class and subject."""
        ...

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
        search: str = "",
        status: str | None = None,
        school_subject_id: UUID | None = None,
    ) -> list[PublicationSummary]: ...
