from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class PendingInvitation:
    id: UUID
    activation_id: UUID
    user_id: UUID
    school_id: UUID
    issuer_id: UUID | None
    email: str
    queue_expires_at: datetime
    attempts: int


InvitationState = Literal[
    "not_requested",
    "requires_assistance",
    "active",
    "pending",
    "sent",
    "failed",
    "expired",
    "activated",
    "superseded",
]


@dataclass(frozen=True)
class InvitationRecipient:
    user_id: UUID
    email: str | None
    has_real_email: bool
    onboarding_required: bool


@dataclass(frozen=True)
class InvitationAdmission:
    user_id: UUID
    notification_id: UUID | None
    queued: bool
    reason: str


@dataclass(frozen=True)
class InvitationStatus:
    user_id: UUID
    notification_id: UUID | None
    state: InvitationState
    reason: str | None
    created_at: datetime | None
    sent_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True)
class InvitationPage:
    items: list[InvitationStatus]
    counts: dict[InvitationState, int]
    total: int
    next_cursor: UUID | None


class ActivationsRepo(Protocol):
    async def request_recipients(
        self, school_id: UUID, user_ids: list[UUID], *, lock: bool = False
    ) -> list[InvitationRecipient]: ...

    async def enable_onboarding(self, user_id: UUID, email: str) -> bool: ...

    async def request_invitation(
        self,
        user_id: UUID,
        school_id: UUID,
        actor_id: UUID,
        now: datetime,
        expires_at: datetime,
        *,
        resend: bool,
        cooldown: timedelta,
    ) -> InvitationAdmission: ...

    async def list_invitations(
        self, school_id: UUID, now: datetime, cursor: UUID | None, limit: int
    ) -> InvitationPage: ...

    async def get_invitation(self, notification_id: UUID) -> PendingInvitation | None: ...

    async def eligible(self, invitation: PendingInvitation) -> bool: ...

    async def reserve_due(self, now: datetime, dispatch_until: datetime, limit: int) -> list[UUID]:
        """Recover stale submissions and reserve due messages with their queue transaction."""
        ...

    async def claim(
        self,
        notification_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
        *,
        sender_key: str,
        hourly_limit: int,
        daily_limit: int,
        sender_spacing: timedelta,
        max_attempts: int,
    ) -> PendingInvitation | None: ...

    async def mark_submitting(
        self,
        notification_id: UUID,
        token: UUID,
        now: datetime,
        link_lifetime: timedelta,
        sender_key: str,
    ) -> bool:
        """Persist the submission fence and activation expiry before the HTTP request."""
        ...

    async def finish(
        self,
        notification_id: UUID,
        token: UUID,
        status: str,
        now: datetime,
        *,
        outcome: str,
        category: str | None = None,
        retry_at: datetime | None = None,
        sender_hold_until: datetime | None = None,
        sender_suspended: bool = False,
    ) -> bool: ...

    async def queue_initial(
        self,
        user_id: UUID,
        school_id: UUID,
        actor_id: UUID,
        now: datetime,
        expires_at: datetime,
    ) -> UUID | None:
        """Atomically queue the first eligible invitation and return its notification ID."""
        ...
