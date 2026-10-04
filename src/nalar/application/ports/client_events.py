from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ClientEvent:
    event_id: UUID
    kind: Literal["error", "vital"]
    code: str
    route: str
    value: float | None
    occurred_at: datetime


class ClientEventsRepo(Protocol):
    async def insert(
        self, actor_id: UUID, events: list[ClientEvent], now: datetime, per_minute: int
    ) -> int: ...
