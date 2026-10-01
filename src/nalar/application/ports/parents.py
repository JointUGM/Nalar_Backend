from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class Child:
    student_id: UUID
    name: str
    school_name: str


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


class ParentsRepo(Protocol):
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
