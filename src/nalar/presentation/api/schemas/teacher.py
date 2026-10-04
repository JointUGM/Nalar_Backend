from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from nalar.presentation.api.schemas.common import Body


class AttemptGrantIn(Body):
    student_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    opens_at: AwareDatetime | None = None
    closes_at: AwareDatetime | None = None


class AttemptGrantOut(BaseModel):
    grant_id: UUID
    run_id: UUID


class DashboardWeekOut(BaseModel):
    week_start: datetime
    week_end: datetime
    sessions_completed: int
    students: int
    active_misconceptions: int
    concepts_with_misconceptions: int
    changed_mind_rate: float | None
    open_flags: int


class DashboardTrendOut(BaseModel):
    week_start: datetime
    mastered: int
    developing: int
    misconception: int


class ChangedMisconceptionOut(BaseModel):
    misconception_id: UUID
    statement: str
    held: int
    resolved: int


class TeacherDashboardOut(BaseModel):
    this_week: DashboardWeekOut
    last_week: DashboardWeekOut
    trend: list[DashboardTrendOut]
    top_changed: list[ChangedMisconceptionOut]
    as_of: datetime
    timezone: str


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


class PublicationWindowIn(Body):
    opens_at: AwareDatetime
    closes_at: AwareDatetime

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.opens_at >= self.closes_at:
            raise ValueError("opens_at must precede closes_at")
        return self


class PublicationRunDetailOut(BaseModel):
    id: UUID
    kind: Literal["primary", "grant"]
    mode: Literal["live", "window"]
    status: str
    opens_at: datetime | None
    closes_at: datetime | None
    join_code: str | None


class PublicationDetailOut(BaseModel):
    id: UUID
    school_id: UUID
    class_id: UUID
    class_name: str
    published_by: UUID
    created_at: datetime
    cancelled_at: datetime | None
    released_to_parents_at: datetime | None
    mission_version_id: UUID
    version_number: int
    mission_id: UUID
    mission_title: str
    knowledge_base_id: UUID
    runs: list[PublicationRunDetailOut]


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
