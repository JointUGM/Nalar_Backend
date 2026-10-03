from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class CredentialSnapshot:
    revision: int
    blocked: bool


class CredentialState(Protocol):
    async def snapshot(self, user_id: UUID) -> CredentialSnapshot: ...

    async def login_revision(self, email: str) -> int: ...


@dataclass(frozen=True)
class PasswordRecipient:
    user_id: UUID
    email: str


@dataclass(frozen=True)
class PendingReset:
    id: UUID
    user_id: UUID
    email: str
    attempts: int
    queue_expires_at: datetime


class PasswordResetsRepo(Protocol):
    async def recipient(self, user_id: UUID, *, recovery: bool) -> PasswordRecipient | None: ...

    async def request(
        self, email: str, now: datetime, expires_at: datetime, cooldown: timedelta
    ) -> UUID | None: ...

    async def reserve_due(
        self, now: datetime, dispatch_until: datetime, limit: int
    ) -> list[UUID]: ...

    async def claim_delivery(
        self,
        reset_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
        sender_key: str,
        hourly_limit: int,
        daily_limit: int,
        sender_spacing: timedelta,
        max_attempts: int,
    ) -> PendingReset | None: ...

    async def mark_submitting(
        self,
        reset: PendingReset,
        token: UUID,
        now: datetime,
        link_lifetime: timedelta,
        sender_key: str,
    ) -> bool: ...

    async def finish_delivery(
        self,
        reset_id: UUID,
        token: UUID,
        now: datetime,
        *,
        accepted: bool,
        category: str | None = None,
        retry_at: datetime | None = None,
        suspended: bool = False,
    ) -> None: ...

    async def claim_proof(
        self, reset_id: UUID, digest: str, token: UUID, now: datetime, lease_until: datetime
    ) -> PasswordRecipient | None: ...

    async def fail_proof(self, reset_id: UUID, token: UUID) -> None: ...

    async def begin_mutation(
        self,
        user_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
        reset_id: UUID | None = None,
    ) -> bool: ...

    async def mutation_phase(self, token: UUID, phase: str, now: datetime) -> bool: ...

    async def reconcile_candidate(self, user_id: UUID, now: datetime) -> UUID | None: ...
