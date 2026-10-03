from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from nalar.application.ports.ai_contract import (
    CandidateIn,
    ChunkOut,
    ConceptDraftOut,
    CpCandidateIn,
    LibraryCandidateIn,
    MisconceptionOut,
    SectionOut,
)


@dataclass(frozen=True)
class KbRef:
    id: UUID
    school_id: UUID
    school_subject_id: UUID
    owner_teacher_id: UUID


@dataclass(frozen=True)
class NewMaterial:
    id: UUID
    title: str
    storage_path: str
    size_bytes: int


@dataclass(frozen=True)
class MaterialForDetect:
    id: UUID
    school_id: UUID
    knowledge_base_id: UUID
    title: str
    storage_bucket: str
    storage_path: str


@dataclass(frozen=True)
class KbSummary:
    id: UUID
    topic_key: str
    topic_title: str
    school_subject_id: UUID
    owner_teacher_id: UUID
    material_count: int
    built_section_count: int
    pending_count: int
    approved_concept_count: int
    created_at: datetime


@dataclass(frozen=True)
class MaterialView:
    id: UUID
    title: str
    page_count: int | None
    pages_without_text: tuple[int, ...]
    archived_at: datetime | None


@dataclass(frozen=True)
class PageSource:
    page_start: int
    page_end: int


@dataclass(frozen=True)
class ConceptView:
    id: UUID
    name: str
    description: str | None
    review_status: str
    cp_learning_outcome_id: UUID | None
    source_chunk_ids: tuple[UUID, ...]
    sources: tuple[PageSource, ...] = ()


@dataclass(frozen=True)
class MisconceptionView:
    id: UUID
    concept_id: UUID
    statement: str
    correct_understanding: str
    detection_cues: tuple[str, ...]
    counter_examples: tuple[str, ...]
    review_status: str
    source_chunk_ids: tuple[UUID, ...]
    sources: tuple[PageSource, ...] = ()


@dataclass(frozen=True)
class KbDetail:
    id: UUID
    topic_title: str
    owner_teacher_id: UUID
    materials: tuple[MaterialView, ...]
    concepts: tuple[ConceptView, ...]
    prerequisites: tuple[tuple[UUID, UUID], ...]
    misconceptions: tuple[MisconceptionView, ...]


@dataclass(frozen=True)
class SectionView:
    id: UUID
    material_id: UUID
    parent_section_id: UUID | None
    ordinal: int
    level: int
    title: str
    page_start: int
    page_end: int
    build_status: str
    built_at: datetime | None
    suggested: bool


@dataclass(frozen=True)
class SectionForBuild:
    id: UUID
    knowledge_base_id: UUID
    school_id: UUID
    build_status: str
    job_id: UUID | None
    job_updated_at: datetime | None


@dataclass(frozen=True)
class BuildContext:
    section_id: UUID
    school_id: UUID
    knowledge_base_id: UUID
    ordinal: int
    title: str
    page_start: int
    page_end: int
    next_title: str | None
    storage_bucket: str
    storage_path: str
    subject: str
    phase: str
    cp_subject_id: UUID | None
    has_chunks: bool
    has_concepts: bool


@dataclass(frozen=True)
class ChunkRow:
    id: UUID
    content: str
    heading_path: str | None
    kind: str
    page_start: int | None
    page_end: int | None
    embedding_model: str | None


@dataclass(frozen=True)
class StoredConcept:
    id: UUID
    name: str
    description: str | None
    embedding: list[float]
    source_chunk_ids: tuple[UUID, ...]
    embedding_model: str | None


type ItemKind = Literal["concept", "misconception"]
type ReviewOutcome = Literal["reviewed", "not_pending", "concept_not_approved"]


@dataclass(frozen=True)
class ItemRef:
    id: UUID
    kind: ItemKind
    knowledge_base_id: UUID
    school_id: UUID
    review_status: str
    reviewed_at: datetime | None
    name: str
    description: str | None


@dataclass(frozen=True)
class ItemPatch:
    name: str | None = None
    description: str | None = None
    statement: str | None = None
    correct_understanding: str | None = None
    detection_cues: tuple[str, ...] | None = None
    counter_examples: tuple[str, ...] | None = None


class KnowledgeRepo(Protocol):
    async def claim_build(
        self, ctx: BuildContext, job_id: UUID, now: datetime, stale_after_s: float
    ) -> int | None:
        """Lease one build per KB; raise BuildBusy while another fresh lease exists."""
        ...

    async def touch_build(self, job_id: UUID, attempt: int) -> bool:
        """Fence writes from expired or superseded workers and refresh the lease."""
        ...

    async def release_build(self, job_id: UUID, attempt: int) -> None: ...

    async def kb_ref(self, kb_id: UUID) -> KbRef | None: ...

    async def topic_exists(self, school_subject_id: UUID, key: str) -> bool: ...

    async def create_kb(self, kb: KbRef, key: str, title: str) -> bool:
        """False when (subject, topic_key) was taken meanwhile."""
        ...

    async def add_material(self, kb: KbRef, uploader_id: UUID, material: NewMaterial) -> None: ...

    async def material_for_detect(self, material_id: UUID) -> MaterialForDetect | None: ...

    async def has_sections(self, material_id: UUID) -> bool: ...

    async def store_detection(
        self,
        material: MaterialForDetect,
        page_count: int,
        pages_without_text: Sequence[int],
        sections: Sequence[SectionOut],
    ) -> None: ...

    async def mark_material_failed(self, material_id: UUID) -> None: ...

    async def kb_page(
        self,
        actor_id: UUID,
        school_id: UUID,
        school_subject_id: UUID | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[KbSummary]:
        """KBs the actor owns or whose subject they teach."""
        ...

    async def kb_detail(self, kb_id: UUID, approved_only: bool) -> KbDetail | None: ...

    async def sections(self, kb_id: UUID) -> list[SectionView]: ...

    async def section_for_build(self, section_id: UUID) -> SectionForBuild | None:
        """Locks every section of the section's material, so overlapping requests serialize."""
        ...

    async def overlaps(self, section_id: UUID) -> bool:
        """An ancestor or descendant is queued, building or built."""
        ...

    async def queue_section(self, section_id: UUID) -> None: ...

    async def build_context(self, section_id: UUID, default_phase: str) -> BuildContext | None: ...

    async def set_build_status(
        self, section_id: UUID, status: str, built_at: datetime | None
    ) -> None: ...

    async def insert_chunks(
        self, ctx: BuildContext, chunks: Sequence[ChunkOut], embedding_model: str
    ) -> None: ...

    async def section_chunks(self, section_id: UUID) -> list[ChunkRow]: ...

    async def kb_concepts(self, kb_id: UUID) -> list[tuple[UUID, str, str | None]]:
        """Active, not rejected: what extraction must not propose again."""
        ...

    async def kb_edges(self, kb_id: UUID) -> list[tuple[UUID, UUID]]: ...

    async def rejected_concepts(self, kb_id: UUID) -> list[tuple[str, str | None]]: ...

    async def match_concepts(
        self, ctx: BuildContext, embedding: Sequence[float], model: str
    ) -> list[CandidateIn]: ...

    async def apply_concepts(
        self,
        ctx: BuildContext,
        links: Sequence[tuple[UUID, Sequence[UUID]]],
        creates: Sequence[ConceptDraftOut],
        model: str,
        linked_keys: dict[str, UUID] | None = None,
    ) -> None: ...

    async def concepts_to_align(self, ctx: BuildContext) -> list[StoredConcept]:
        """Pending AI concepts sourced from this section with no CP label yet."""
        ...

    async def match_cp(
        self, cp_subject_id: UUID, embedding: Sequence[float], model: str
    ) -> list[CpCandidateIn]: ...

    async def set_cp_outcome(
        self, ctx: BuildContext, concept_id: UUID, outcome_id: UUID
    ) -> None: ...

    async def concepts_without_misconceptions(self, ctx: BuildContext) -> list[StoredConcept]: ...

    async def match_library(
        self, ctx: BuildContext, embedding: Sequence[float], model: str
    ) -> list[LibraryCandidateIn]: ...

    async def insert_misconceptions(
        self, ctx: BuildContext, rows: Sequence[MisconceptionOut], model: str
    ) -> None: ...

    async def item_ref(
        self, kind: ItemKind, item_id: UUID, *, lock: bool = False
    ) -> ItemRef | None: ...

    async def review_item(
        self, kind: ItemKind, item_id: UUID, status: str, actor_id: UUID, now: datetime
    ) -> ReviewOutcome: ...

    async def edit_item(
        self,
        kind: ItemKind,
        item_id: UUID,
        patch: ItemPatch,
        embedding: Sequence[float] | None,
        model: str | None,
    ) -> bool:
        """Only while pending; the new vector replaces the old one with its model name."""
        ...

    async def concept_view(self, concept_id: UUID) -> ConceptView | None: ...

    async def misconception_view(self, misconception_id: UUID) -> MisconceptionView | None: ...

    async def review_queue(self, kb_id: UUID) -> tuple[int, int]: ...
