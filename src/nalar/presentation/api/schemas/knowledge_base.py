from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from nalar.presentation.api.schemas.common import Body


class MaterialQueuedOut(BaseModel):
    knowledge_base_id: UUID
    material_id: UUID
    job_id: UUID
    status: str = "queued"


class KbSummaryOut(BaseModel):
    owner_name: str | None
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


class PageSourceOut(BaseModel):
    page_start: int
    page_end: int


class ConceptOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    review_status: str
    cp_learning_outcome_id: UUID | None
    source_chunk_ids: list[UUID]
    sources: list[PageSourceOut]


class PrerequisiteOut(BaseModel):
    concept_id: UUID
    prerequisite_concept_id: UUID


type ItemText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
]


class ConceptCreateIn(Body):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    description: ItemText | None = None
    source_chunk_ids: list[UUID] = Field(default_factory=list, max_length=100)


class MisconceptionCreateIn(Body):
    statement: ItemText
    correct_understanding: ItemText
    detection_cues: list[ItemText] = Field(default_factory=list, max_length=30)
    counter_examples: list[ItemText] = Field(default_factory=list, max_length=30)
    source_chunk_ids: list[UUID] = Field(default_factory=list, max_length=100)


class MisconceptionOut(BaseModel):
    id: UUID
    concept_id: UUID
    statement: str
    correct_understanding: str
    detection_cues: list[str]
    counter_examples: list[str]
    review_status: str
    source_chunk_ids: list[UUID]
    sources: list[PageSourceOut]


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


class ReviewIn(Body):
    review_status: Literal["approved", "rejected"]


class ReviewOut(BaseModel):
    id: UUID
    review_status: str
    reviewed_at: datetime


class _ItemPatchIn(Body):
    model_config = ConfigDict(str_strip_whitespace=True)

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Kirim setidaknya satu bidang; nilai null tidak diterima.")
        return self


class ConceptPatchIn(_ItemPatchIn):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)


class MisconceptionPatchIn(_ItemPatchIn):
    statement: str | None = Field(default=None, min_length=1, max_length=1000)
    correct_understanding: str | None = Field(default=None, min_length=1, max_length=2000)
    detection_cues: list[Annotated[str, StringConstraints(min_length=1, max_length=300)]] | None = (
        Field(default=None, max_length=20)
    )
    counter_examples: (
        list[Annotated[str, StringConstraints(min_length=1, max_length=2000)]] | None
    ) = Field(default=None, max_length=20)


class ReviewQueueOut(BaseModel):
    knowledge_base_id: UUID
    pending_concepts: int
    pending_misconceptions: int
