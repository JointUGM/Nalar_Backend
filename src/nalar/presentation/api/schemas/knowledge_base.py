from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MaterialQueuedOut(BaseModel):
    knowledge_base_id: UUID
    material_id: UUID
    job_id: UUID
    status: str = "queued"


class KbSummaryOut(BaseModel):
    id: UUID
    topic_key: str
    topic_title: str
    school_subject_id: UUID
    owner_teacher_id: UUID
    material_count: int
    built_section_count: int
    pending_count: int
    approved_concept_count: int
    can_edit: bool


class KbPageOut(BaseModel):
    items: list[KbSummaryOut]
    next_cursor: str | None


class MaterialOut(BaseModel):
    id: UUID
    title: str
    page_count: int | None
    pages_without_text: list[int]
    archived_at: datetime | None


class ConceptOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    review_status: str
    cp_learning_outcome_id: UUID | None
    source_chunk_ids: list[UUID]


class PrerequisiteOut(BaseModel):
    concept_id: UUID
    prerequisite_concept_id: UUID


class MisconceptionOut(BaseModel):
    id: UUID
    concept_id: UUID
    statement: str
    correct_understanding: str
    detection_cues: list[str]
    counter_examples: list[str]
    review_status: str
    source_chunk_ids: list[UUID]


class KbDetailOut(BaseModel):
    id: UUID
    topic_title: str
    owner_teacher_id: UUID
    materials: list[MaterialOut]
    concepts: list[ConceptOut]
    prerequisites: list[PrerequisiteOut]
    misconceptions: list[MisconceptionOut]
    can_edit: bool


class SectionItemOut(BaseModel):
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


class SectionsOut(BaseModel):
    items: list[SectionItemOut]


class BuildQueuedOut(BaseModel):
    job_id: UUID
    section_id: UUID
    status: str = "queued"
