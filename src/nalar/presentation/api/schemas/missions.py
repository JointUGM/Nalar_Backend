from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from nalar.presentation.api.schemas.common import Body

Descriptors = list[str]


class MissionIn(Body):
    knowledge_base_id: UUID
    title: str = Field(min_length=1, max_length=200)
    learning_objective: str = Field(min_length=1, max_length=1000)


class MissionCreatedOut(BaseModel):
    mission_id: UUID


class MissionGenerationQueuedOut(BaseModel):
    job_id: UUID
    status: str = "queued"


class RubricIn(Body):
    claim: Descriptors = Field(min_length=5, max_length=5)
    evidence: Descriptors = Field(min_length=5, max_length=5)
    mechanism: Descriptors = Field(min_length=5, max_length=5)
    transfer: Descriptors = Field(min_length=5, max_length=5)


class BankQuestionIn(Body):
    id: str = Field(min_length=1, max_length=64)
    concept_id: UUID
    misconception_id: UUID | None = None
    move: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=1000)


class WarmupChoiceIn(Body):
    id: str = Field(min_length=1, max_length=8)
    text: str = Field(min_length=1, max_length=300)


class LiveWarmupIn(Body):
    prompt: str = Field(min_length=1, max_length=500)
    choices: list[WarmupChoiceIn] = Field(min_length=2, max_length=5)


class VersionIn(Body):
    base_version_id: UUID | None = None
    anchor_problem: str = Field(min_length=1, max_length=4000)
    rubric: RubricIn
    target_concept_ids: list[UUID] = Field(min_length=1, max_length=5)
    misconception_ids: list[UUID] = Field(default_factory=list, max_length=30)
    question_bank: list[BankQuestionIn] = Field(min_length=1, max_length=200)
    answer_terms: list[str] = Field(min_length=1, max_length=50)
    reference_reasoning: str = Field(min_length=1, max_length=8000)
    source_chunk_ids: list[UUID] = Field(default_factory=list, max_length=200)
    live_warmup: LiveWarmupIn | None = None
    max_turns: int = Field(ge=2, le=10)
    max_duration_minutes: int = Field(default=20, ge=5, le=60)


class VersionCreatedOut(BaseModel):
    version_id: UUID
    version_number: int
    status: str = "draft"


class VersionReviewedOut(BaseModel):
    version_id: UUID
    status: str = "reviewed"
    reviewed_at: datetime


class VersionOut(BaseModel):
    id: UUID
    version_number: int
    status: str
    anchor_problem: str
    rubric: dict[str, list[str]]
    target_concept_ids: list[UUID]
    misconception_ids: list[UUID]
    question_bank: list[dict[str, Any]]
    answer_terms: list[str]
    reference_reasoning: str
    source_chunk_ids: list[UUID]
    live_warmup: dict[str, Any] | None
    max_turns: int
    max_duration_minutes: int
    can_edit: bool


class VersionSummaryOut(BaseModel):
    id: UUID
    version_number: int
    status: str


class MissionSummaryOut(BaseModel):
    id: UUID
    title: str
    knowledge_base_id: UUID
    created_by: UUID
    latest_version: VersionSummaryOut | None
    can_edit: bool


class MissionPageOut(BaseModel):
    items: list[MissionSummaryOut]
    next_cursor: str | None
