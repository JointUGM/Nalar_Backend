from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from nalar.domain.release import ReleaseCounts


@dataclass(frozen=True)
class PublicationText:
    id: UUID
    school_id: UUID
    mission_title: str
    released_at: datetime | None


@dataclass(frozen=True)
class SummaryPreview:
    student_id: UUID
    name: str
    summary_text: str


@dataclass(frozen=True)
class SummaryConcept:
    name: str
    outcome: str
    misconception_statement: str | None
    resolved_in_session: bool


@dataclass(frozen=True)
class SummaryInput:
    student_id: UUID
    concepts: tuple[SummaryConcept, ...]
    evaluation_summary: str | None


@dataclass(frozen=True)
class StoredInsight:
    narrative: str
    counts_snapshot: Mapping[str, Any]
    generated_at: datetime
    suggestions: tuple[str, ...] = ()


class ReleaseRepo(Protocol):
    async def publication_text(self, publication_id: UUID) -> PublicationText | None: ...

    async def lock(self, publication_id: UUID) -> PublicationText | None:
        """The publication row FOR UPDATE: concurrent releases serialize here (TC-18)."""
        ...

    async def counts(self, publication_id: UUID) -> ReleaseCounts: ...

    async def preview_summaries(self, publication_id: UUID) -> list[SummaryPreview]:
        """Eligible students' stored summaries only."""
        ...

    async def release(self, publication_id: UUID, actor_id: UUID, now: datetime) -> None: ...

    async def summary_input(self, publication_id: UUID) -> list[SummaryInput]:
        """Eligible students without a summary, with what S5 may see (no names, ids or scores)."""
        ...

    async def insert_summary(
        self,
        school_id: UUID,
        publication_id: UUID,
        student_id: UUID,
        text: str,
        ai_invocation_id: UUID | None,
    ) -> bool:
        """AI-5: the first success per (publication, student), and never after release."""
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
    ) -> None: ...

    async def latest_insight(self, publication_id: UUID) -> StoredInsight | None: ...

    async def finalize_runs(self, publication_id: UUID) -> int:
        """Succeeded finalizer jobs so far; the insight is tried in at most two of them."""
        ...

    async def reminder_candidates(self, finalized_before: datetime) -> list[tuple[UUID, UUID]]:
        """(publication, school) unreleased, finalized before the cutoff."""
        ...
