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
    ) -> bool:
        """NFR-R1: repeated (session_id, client_seq) ignored; returns true if inserted."""
        ...
