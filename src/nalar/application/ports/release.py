from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class PublicationText:
    id: UUID
    school_id: UUID
    mission_title: str


@dataclass(frozen=True)
class StoredInsight:
    narrative: str
    counts_snapshot: Mapping[str, Any]
    generated_at: datetime
    suggestions: tuple[str, ...] = ()


class ReleaseRepo(Protocol):
    async def publication_text(self, publication_id: UUID) -> PublicationText | None: ...

    async def lock(self, publication_id: UUID) -> PublicationText | None:
        """Serialize class insight persistence on the publication row."""
        ...

    async def has_insight(self, publication_id: UUID) -> bool: ...

    async def insert_insight(
        self,
        school_id: UUID,
        publication_id: UUID,
        counts_snapshot: Mapping[str, Any],
        clusters: Sequence[Mapping[str, Any]],
        narrative: str,
        ai_invocation_id: UUID | None,
        suggestions: Sequence[str],
    ) -> None: ...

    async def latest_insight(self, publication_id: UUID) -> StoredInsight | None: ...

    async def finalize_runs(self, publication_id: UUID) -> int:
        """Succeeded finalizer jobs so far; the insight is tried in at most two of them."""
        ...
