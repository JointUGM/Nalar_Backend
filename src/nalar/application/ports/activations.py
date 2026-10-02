from datetime import datetime
from typing import Protocol
from uuid import UUID


class ActivationsRepo(Protocol):
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
