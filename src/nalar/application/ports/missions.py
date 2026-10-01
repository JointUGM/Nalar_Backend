from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from nalar.domain.context_pack import PackMisconception, Target


def version_status(reviewed_at: datetime | None, locked_at: datetime | None) -> str:
    if locked_at is not None:
        return "locked"
    return "reviewed" if reviewed_at is not None else "draft"


@dataclass(frozen=True)
class MissionRef:
    id: UUID
    school_id: UUID
    knowledge_base_id: UUID
    school_subject_id: UUID
    created_by: UUID


@dataclass(frozen=True)
class VersionDraft:
    anchor_problem: str
    rubric: Mapping[str, Sequence[str]]
    target_concept_ids: tuple[UUID, ...]
    misconception_ids: tuple[UUID, ...]
    question_bank: tuple[Mapping[str, Any], ...]
    answer_terms: tuple[str, ...]
    reference_reasoning: str
    source_chunk_ids: tuple[UUID, ...]
    live_warmup: Mapping[str, Any] | None
    max_turns: int
    max_duration_minutes: int


@dataclass(frozen=True)
class VersionRecord:
    id: UUID
    mission_id: UUID
    version_number: int
    status: str
    draft: VersionDraft
    created_by: UUID
    reviewed_at: datetime | None


@dataclass(frozen=True)
class VersionSummary:
    id: UUID
    version_number: int
    status: str


@dataclass(frozen=True)
class MissionSummary:
    id: UUID
    title: str
    knowledge_base_id: UUID
    created_by: UUID
    latest_version: VersionSummary | None
    created_at: datetime


class MissionsRepo(Protocol):
    async def kb_school_id(self, kb_id: UUID) -> UUID | None: ...

    async def mission_ref(self, mission_id: UUID) -> MissionRef | None: ...

    async def create_mission(
        self, school_id: UUID, kb_id: UUID, actor_id: UUID, title: str, objective: str
    ) -> UUID: ...

    async def next_version_number(self, mission_id: UUID) -> int:
        """Locks the mission row, so two concurrent drafts get distinct numbers."""
        ...

    async def item_problems(
        self,
        kb_id: UUID,
        school_id: UUID,
        concept_ids: Sequence[UUID],
        misconception_ids: Sequence[UUID],
        chunk_ids: Sequence[UUID],
    ) -> list[tuple[str, str]]:
        """(code, id) for every id that isn't an approved, active item or chunk of the KB."""
        ...

    async def insert_version(
        self,
        mission: MissionRef,
        number: int,
        actor_id: UUID,
        draft: VersionDraft,
        probe_plan: Mapping[str, Sequence[str]],
    ) -> UUID: ...

    async def version(self, mission_id: UUID, number: int) -> VersionRecord | None: ...

    async def version_items(
        self, version_id: UUID
    ) -> tuple[tuple[Target, ...], tuple[PackMisconception, ...]]:
        """Only items that are still approved and active; the caller compares the counts."""
        ...

    async def mark_reviewed(
        self, version_id: UUID, pack: Mapping[str, Any], actor_id: UUID, now: datetime
    ) -> bool:
        """False unless the version was an unlocked, unreviewed draft."""
        ...

    async def mission_page(
        self,
        actor_id: UUID,
        school_id: UUID,
        school_subject_id: UUID | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[MissionSummary]:
        """The actor's own missions, plus missions with a reviewed version in a subject the
        actor teaches; for others' missions the latest version is the latest reviewed one."""
        ...
