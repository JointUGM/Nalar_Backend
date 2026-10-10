from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from nalar.application.ports.ai_contract import RevisionFeedback
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


class MissionRevisionIn(Body):
    base_version_id: UUID
    expected_latest_version_id: UUID
    feedback: list[RevisionFeedback] = Field(default_factory=list, max_length=8)
    learning_objective: str | None = Field(default=None, min_length=1, max_length=1000)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    target_concept_ids: list[UUID] | None = Field(default=None, min_length=2, max_length=3)

    @model_validator(mode="after")
    def validate_overrides(self) -> "MissionRevisionIn":
        for item in self.feedback:
            if item.question_ids is None:
                if "question_ids" in item.model_fields_set:
                    raise ValueError("question_ids must be an array")
                item.question_ids = []
            if not item.desired_change.strip():
                raise ValueError("feedback must describe the desired change")
            item.desired_change = item.desired_change.strip()
            if item.question_ids and item.component != "bank":
                raise ValueError("question_ids are only valid for bank feedback")
            if item.question_ids and len(set(item.question_ids)) != len(item.question_ids):
                raise ValueError("question_ids must be unique")
        for name in ("learning_objective", "title", "target_concept_ids"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must be omitted to retain its value")
        for name in ("learning_objective", "title"):
            value = getattr(self, name)
            if value is not None:
                if not value.strip():
                    raise ValueError(f"{name} must not be blank")
                setattr(self, name, value.strip())
        if self.target_concept_ids is not None and len(set(self.target_concept_ids)) != len(
            self.target_concept_ids
        ):
            raise ValueError("target_concept_ids must be unique")
        return self


class MissionRevisionQueuedOut(MissionGenerationQueuedOut):
    base_version_id: UUID
    effective_scope: list[str]


class MissionRevisionRequestOut(BaseModel):
    job_id: UUID
    status: str
    intent: MissionRevisionIn
    effective_scope: list[str]


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
    learning_objective: str
    title: str
    base_version_id: UUID | None
    base_version_number: int | None
    revision_job_id: UUID | None
    revision_feedback: list[RevisionFeedback]
    revision_changed_fields: list[str]
    can_revise_with_ai: bool


class VersionSummaryOut(BaseModel):
    id: UUID
    version_number: int
    status: str


class MissionSummaryOut(BaseModel):
    created_by_name: str | None
    id: UUID
    title: str
    knowledge_base_id: UUID
    created_by: UUID
    latest_version: VersionSummaryOut | None
    can_edit: bool


class MissionPageOut(BaseModel):
    items: list[MissionSummaryOut]
    next_cursor: str | None


class VersionHistoryOut(BaseModel):
    version_number: int
    status: str
    created_at: datetime
    created_by_name: str | None
    reviewed_at: datetime | None
    locked_at: datetime | None
