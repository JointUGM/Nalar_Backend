from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

from nalar.presentation.api.schemas.common import Body

type Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30000)]
type Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class SourceStatementIn(Body):
    description: Text
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)


class SourceElementIn(SourceStatementIn):
    element: Label
    statements: list[SourceStatementIn] = Field(min_length=1, max_length=2048)


class ReferenceSubjectIn(Body):
    name: Label
    phase: Literal["A", "B", "C", "D", "E", "F"]
    elements: list[SourceElementIn] = Field(min_length=1, max_length=100)


class ReferenceCurriculumIn(Body):
    name: Label
    decree_code: Label
    effective_on: date
    is_current: bool = True
    subjects: list[ReferenceSubjectIn] = Field(min_length=1, max_length=100)


class ReferenceReviewDraft(Body):
    curriculum: ReferenceCurriculumIn | None = None
    selected_pages: list[int] = Field(default_factory=list, max_length=500)


class ReferenceReviewIn(ReferenceReviewDraft):
    base_revision: int = Field(ge=0)


class ReferenceRevisionIn(Body):
    base_revision: int = Field(ge=0)


class ReferenceRevisionOut(BaseModel):
    revision: int


class ReferenceQueuedOut(BaseModel):
    document_id: UUID
    job_id: UUID
    status: Literal["extracting", "indexing"]


class ReferenceSummaryOut(BaseModel):
    id: UUID
    kind: Literal["curriculum", "guidance"]
    title: str
    issuer: str
    source_url: str
    sha256: str
    status: Literal["uploading", "extracting", "review", "indexing", "published", "failed"]
    revision: int
    created_at: datetime
    published_at: datetime | None
    curriculum_version_id: UUID | None
    job_id: UUID | None
    error_code: str | None


class ReferencePageOut(BaseModel):
    page_number: int
    text: str


class ReferenceDetailOut(ReferenceSummaryOut):
    review: ReferenceReviewDraft | None
    pages: list[ReferencePageOut]
