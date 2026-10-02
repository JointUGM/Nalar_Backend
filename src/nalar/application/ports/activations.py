from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
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


class ActivationsRepo(Protocol):
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
