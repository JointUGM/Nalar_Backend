from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, model_validator

from nalar.presentation.api.schemas.common import Body


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
