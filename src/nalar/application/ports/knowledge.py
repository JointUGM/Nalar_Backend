from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from nalar.application.ports.ai_contract import SectionOut


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
class ConceptView:
    id: UUID
    name: str
    description: str | None
    review_status: str
    cp_learning_outcome_id: UUID | None
    source_chunk_ids: tuple[UUID, ...]


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


class KnowledgeRepo(Protocol):
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
