from datetime import datetime
from typing import Protocol
from uuid import UUID

from nalar.application.ports.administration import AdminRow


class NotificationsRepo(Protocol):
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

    async def inbox(
        self, actor_id: UUID, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[AdminRow]: ...

    async def unread_count(self, actor_id: UUID) -> int: ...

    async def read(self, actor_id: UUID, notification_id: UUID, now: datetime) -> bool: ...
