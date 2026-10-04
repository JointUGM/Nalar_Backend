from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

from nalar.presentation.api.schemas.common import Body


class MaterialFileOut(BaseModel):
    url: str
    expires_in: int


class NotificationOut(BaseModel):
    id: UUID
    type: str
    created_at: datetime
    read_at: datetime | None


class NotificationsOut(BaseModel):
    items: list[NotificationOut]
    unread_count: int
    next_cursor: str | None


class ClientEventIn(Body):
    event_id: UUID
    kind: Literal["error", "vital"]
    code: Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]
    route: Annotated[str, StringConstraints(max_length=160, pattern=r"^/[A-Za-z0-9_/:.\-]*$")]
    value: Annotated[float, Field(allow_inf_nan=False, ge=0, le=1e9)] | None = None
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def vital_value(self) -> Self:
        if self.kind == "vital" and (
            self.code not in ("CLS", "FCP", "FID", "INP", "LCP", "TTFB") or self.value is None
        ):
            raise ValueError("a vital requires a supported metric and numeric value")
        if self.kind == "error" and self.value is not None:
            raise ValueError("errors have no numeric value")
        return self


class ClientEventsIn(Body):
    events: Annotated[list[ClientEventIn], Field(min_length=1, max_length=20)]


class ClientEventsOut(BaseModel):
    accepted: int


class SchoolPatchIn(Body):
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
        | None
    ) = None
    npsn: Annotated[str, StringConstraints(pattern=r"^\d{8}$")] | None = None
    city: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
        | None
    ) = None

    @model_validator(mode="after")
    def fields_required(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("provide at least one non-null school field")
        return self


class AiUsageOut(BaseModel):
    day: date
    school_id: UUID | None
    purpose: str
    model: str
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class AuditItemOut(BaseModel):
    id: int
    school_id: UUID | None
    actor_id: UUID | None
    action: str
    entity_table: str
    entity_id: UUID | None
    created_at: datetime


class AuditPageOut(BaseModel):
    items: list[AuditItemOut]
    next_cursor: int | None


class HistoryScoreOut(BaseModel):
    dimension: str
    final_level: int


class StudentHistoryItemOut(BaseModel):
    session_id: UUID
    publication_id: UUID
    started_at: datetime
    ended_at: datetime | None
    status: str
    attempt_number: int
    mission_title: str
    subject_name: str
    class_name: str
    academic_year_id: UUID
    academic_year_name: str
    evaluation_status: str | None
    scores: list[HistoryScoreOut]


class StudentHistoryOut(BaseModel):
    items: list[StudentHistoryItemOut]
    next_cursor: str | None
