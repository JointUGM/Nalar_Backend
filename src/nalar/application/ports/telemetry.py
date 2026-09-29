from datetime import datetime
from typing import Protocol
from uuid import UUID


class TelemetryRepo(Protocol):
    async def insert(
        self,
        session_id: UUID,
        client_seq: int,
        turn_index: int | None,
        events_json: str,
        client_sent_at: datetime | None,
    ) -> None:
        """NFR-R1: a repeated (session_id, client_seq) is ignored."""
        ...
