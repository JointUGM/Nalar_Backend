from typing import Protocol
from uuid import UUID


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

    async def release_reminder(
        self, recipient_ids: list[UUID], school_id: UUID, publication_id: UUID
    ) -> int:
        """D-S26-15: in-app only, once per teacher per publication; returns how many were new."""
        ...
