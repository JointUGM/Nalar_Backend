from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class PendingDigest:
    id: UUID
    recipient_id: UUID
    dedupe_key: str
    payload: dict[str, Any]


class NotificationsRepo(Protocol):
    async def queue_digest(
        self,
        recipient_id: UUID,
        school_id: UUID,
        dedupe_key: str,
        items: list[dict[str, str]],
        now: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def pending_digests(self, now: datetime) -> list[PendingDigest]: ...

    async def get_digest(self, digest_id: UUID) -> PendingDigest | None: ...

    async def reserve_due_digests(
        self, now: datetime, dispatch_until: datetime, limit: int
    ) -> list[UUID]:
        """Reserve due messages in the same transaction as their queue insertion."""
        ...

    async def mark_digest_submitting(
        self, digest_id: UUID, token: UUID, now: datetime, sender_key: str
    ) -> bool:
        """Fence one potentially accepted submission before SMTP DATA."""
        ...

    async def claim_digest(
        self,
        digest_id: UUID,
        now: datetime,
        lease_until: datetime,
        token: UUID,
        delivery: dict[str, Any],
        *,
        sender_key: str,
        daily_limit: int,
        sender_spacing: timedelta,
    ) -> PendingDigest | None:
        """Atomically lease pending delivery and preserve its first request body."""
        ...

    async def finish_digest(
        self,
        digest_id: UUID,
        token: UUID,
        status: str,
        now: datetime,
        *,
        outcome: Literal["accepted", "rejected", "unknown", "skipped"] = "skipped",
        category: str | None = None,
        retry_at: datetime | None = None,
        sender_hold_until: datetime | None = None,
        sender_suspended: bool = False,
        smtp_code: int | None = None,
        enhanced_code: str | None = None,
    ) -> bool:
        """Fence completion by claim token; pending releases a retryable lease."""
        ...

    async def wellbeing_alert(
        self,
        recipient_ids: list[UUID],
        school_id: UUID,
        session_id: UUID,
        publication_id: UUID,
        turn_index: int,
    ) -> int:
        """One alert per teacher per paused turn (dedupe_key); returns how many were new."""
        ...

    async def release_reminder(
        self, recipient_ids: list[UUID], school_id: UUID, publication_id: UUID
    ) -> int:
        """D-S26-15: in-app only, once per teacher per publication; returns how many were new."""
        ...
