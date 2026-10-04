from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class Child:
    student_id: UUID
    name: str
    school_name: str
    school_id: UUID | None = None
    class_name: str | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True)
class VisibleResult:
    publication_id: UUID
    session_id: UUID
    mission_title: str
    released_at: datetime
    completed_at: datetime
    summary: str


@dataclass(frozen=True)
class ParentReflection:
    session_id: UUID
    mission_title: str
    completed_at: datetime
    content: str
    subject_name: str = ""


@dataclass(frozen=True)
class DigestItem:
    student_id: UUID
    publication_id: UUID
    session_id: UUID
    child_name: str
    mission_title: str
    summary: str


@dataclass(frozen=True)
class DigestRecipient:
    parent_id: UUID
    school_id: UUID
    email: str
    items: tuple[DigestItem, ...]


class ParentsRepo(Protocol):
    async def digest_recipients(
        self,
        released_after: datetime | None,
        parent_id: UUID | None = None,
        *,
        for_submission: bool = False,
    ) -> list[DigestRecipient]:
        """Current opted-in parents and summaries satisfying the app's visibility rule."""
        ...

    async def children(self, parent_id: UUID, limit: int, after: UUID | None) -> list[Child]: ...

    async def visible_results(self, student_id: UUID) -> list[VisibleResult]:
        """Released publications whose latest attempt is completed and evaluated, with a summary."""
        ...

    async def concepts_seen(self, student_id: UUID) -> list[tuple[str, str]]:
        """(concept name, outcome) from visible sessions only, latest observation per concept."""
        ...

    async def reflections(
        self, student_id: UUID, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[ParentReflection]: ...

    async def digest_enabled(self, user_id: UUID) -> bool: ...

    async def set_digest(self, user_id: UUID, enabled: bool) -> None: ...

    async def mark_seen(self, parent_id: UUID, student_id: UUID, now: datetime) -> None: ...
