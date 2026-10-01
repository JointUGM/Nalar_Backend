from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from nalar.presentation.api.schemas.common import Body


class BlockerOut(BaseModel):
    code: str
    count: int


class SummaryPreviewOut(BaseModel):
    student_id: UUID
    name: str
    summary_text: str


class ReleasePreviewOut(BaseModel):
    ready: bool
    blockers: list[BlockerOut]
    eligible_count: int
    ineligible_count: int
    summaries: list[SummaryPreviewOut]
    released_at: datetime | None


class ReleaseIn(Body):
    expected_eligible_count: int = Field(ge=0)


class ReleasedOut(BaseModel):
    released_to_parents_at: datetime
    summary_count: int
