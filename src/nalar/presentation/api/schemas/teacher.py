from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from nalar.presentation.api.schemas.common import Body


class StudentConceptCountsOut(BaseModel):
    mastered: int
    developing: int
    misconception: int


class ClassStudentOut(BaseModel):
    student_id: UUID
    full_name: str
    session_id: UUID | None
    status: (
        Literal[
            "not_started", "in_progress", "paused_safety", "completed", "timed_out", "ended_safety"
        ]
        | None
    )
    completed_at: datetime | None
    concept_counts: StudentConceptCountsOut
    open_flag_count: int
    evaluation_status: Literal["pending", "completed", "failed", "no_answer"] | None


class ClassStudentsOut(BaseModel):
    items: list[ClassStudentOut]
    publication_id: UUID | None


class AttentionEntryOut(BaseModel):
    item_id: UUID
    created_at: datetime


class SafetyAttentionOut(AttentionEntryOut):
    kind: Literal["safety"]
    session_id: UUID
    publication_id: UUID
    student_name: str
    paused_at: datetime | None


class FlagAttentionOut(AttentionEntryOut):
    kind: Literal["flag"]
    flag_id: UUID
    flag_type: str
    severity: str
    session_id: UUID
    publication_id: UUID
    student_name: str


class KbReviewAttentionOut(AttentionEntryOut):
    kind: Literal["kb_review"]
    knowledge_base_id: UUID
    topic_title: str
    pending_concepts: int
    pending_misconceptions: int


class ReleaseReadyAttentionOut(AttentionEntryOut):
    kind: Literal["release_ready"]
    publication_id: UUID
    class_name: str
    mission_title: str
    eligible_count: int


AttentionItemOut = Annotated[
    SafetyAttentionOut | FlagAttentionOut | KbReviewAttentionOut | ReleaseReadyAttentionOut,
    Field(discriminator="kind"),
]


class AttentionCountsOut(BaseModel):
    safety: int
    flag: int
    kb_review: int
    release_ready: int
    total: int


class AttentionPageOut(BaseModel):
    items: list[AttentionItemOut]
    counts: AttentionCountsOut
    next_cursor: str | None


class RoleOut(BaseModel):
    role: str
    school_id: UUID
    school_name: str


class MeOut(BaseModel):
    user_id: UUID
    full_name: str
    roles: list[RoleOut]
    is_parent: bool
    is_platform_admin: bool


class RunIn(Body):
    mode: Literal["window", "live"]
    opens_at: AwareDatetime | None = None
    closes_at: AwareDatetime | None = None
    planner_mode: Literal["table", "hybrid"] | None = None

    @model_validator(mode="after")
    def window_has_times(self) -> Self:
        if self.mode == "window":
            if self.opens_at is None or self.closes_at is None or self.opens_at >= self.closes_at:
                raise ValueError("a window run needs opens_at before closes_at")
        elif self.opens_at is not None or self.closes_at is not None:
            raise ValueError("a live run has no window times")
        return self


class PublishIn(Body):
    mission_version_id: UUID
    class_id: UUID
    run: RunIn


class PublishOut(BaseModel):
    publication_id: UUID
    run_id: UUID
    run_status: str


class AssignmentOut(BaseModel):
    school_id: UUID
    class_id: UUID
    class_name: str
    grade_level: int
    school_subject_id: UUID
    subject_name: str


class AssignmentsOut(BaseModel):
    items: list[AssignmentOut]


class RunSummaryOut(BaseModel):
    id: UUID
    mode: str
    status: str
    opens_at: datetime | None
    closes_at: datetime | None
    join_code: str | None


class CountsOut(BaseModel):
    started: int
    completed: int
    timed_out: int
    evaluated: int


class PublicationOut(BaseModel):
    id: UUID
    mission_title: str
    class_id: UUID
    class_name: str
    run: RunSummaryOut
    counts: CountsOut
    released_to_parents_at: datetime | None


class PublicationsPageOut(BaseModel):
    items: list[PublicationOut]
    next_cursor: str | None
