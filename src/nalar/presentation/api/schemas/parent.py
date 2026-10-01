from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from nalar.presentation.api.schemas.common import Body


class ParentChildOut(BaseModel):
    student_id: UUID
    name: str
    school_name: str


class ParentChildrenOut(BaseModel):
    items: list[ParentChildOut]
    next_cursor: str | None


class ParentSummaryOut(BaseModel):
    publication_id: UUID
    mission_title: str
    released_at: datetime
    text: str


class ParentProgressOut(BaseModel):
    sessions_completed: int
    concepts_understood: list[str]
    concepts_developing: list[str]
    summaries: list[ParentSummaryOut]


class ParentReflectionOut(BaseModel):
    session_id: UUID
    mission_title: str
    completed_at: datetime
    content: str


class ParentReflectionsOut(BaseModel):
    items: list[ParentReflectionOut]
    next_cursor: str | None


class ParentPreferencesIn(Body):
    weekly_digest_enabled: bool


class ParentPreferencesOut(BaseModel):
    weekly_digest_enabled: bool
