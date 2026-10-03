from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from nalar.application.ports.activations import InvitationState


class InvitationRequestIn(BaseModel):
    user_ids: list[UUID] = Field(min_length=1, max_length=100)
    resend: bool = False


class InvitationAdmissionOut(BaseModel):
    user_id: UUID
    notification_id: UUID | None
    queued: bool
    reason: str


class InvitationsQueuedOut(BaseModel):
    queued: int
    skipped: int
    notification_ids: list[UUID]
    items: list[InvitationAdmissionOut]


class InvitationStatusOut(BaseModel):
    user_id: UUID
    full_name: str
    role: str
    notification_id: UUID | None
    state: InvitationState
    reason: str | None
    created_at: datetime | None
    sent_at: datetime | None
    expires_at: datetime | None


class InvitationPageOut(BaseModel):
    items: list[InvitationStatusOut]
    counts: dict[InvitationState, int]
    total: int
    next_cursor: UUID | None
