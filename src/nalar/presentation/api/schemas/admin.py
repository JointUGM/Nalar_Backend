from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


class AcademicYearOut(BaseModel):
    id: UUID
    name: str
    starts_on: date
    ends_on: date
    is_current: bool


class RosterQueuedOut(BaseModel):
    import_id: UUID
    job_id: UUID
    status: str = "pending"


class RowErrorOut(BaseModel):
    row_number: int
    field: str
    message: str


class RosterImportOut(BaseModel):
    status: str
    rows_total: int | None
    rows_succeeded: int | None
    rows_failed: int | None
    errors: list[RowErrorOut]


class RosterImportSummaryOut(BaseModel):
    import_id: UUID
    academic_year_id: UUID
    status: str
    rows_total: int | None
    rows_succeeded: int | None
    rows_failed: int | None
    created_at: datetime
    completed_at: datetime | None


class RosterImportsPageOut(BaseModel):
    items: list[RosterImportSummaryOut]
    next_cursor: UUID | None
    total: int
