from uuid import UUID

from pydantic import BaseModel


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
