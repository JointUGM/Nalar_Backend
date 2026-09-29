from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from nalar.domain.labels import RunMode, RunStatus


@dataclass(frozen=True)
class LockedRun:
    id: UUID
    school_id: UUID
    publication_id: UUID
    mode: RunMode
    status: RunStatus
    join_code: str | None
    lobby_opened_at: datetime | None
    started_at: datetime | None
    closed_at: datetime | None
    anchor_problem: str
    max_duration_minutes: int


@dataclass(frozen=True)
class WarmInput:
    school_id: UUID
    context_pack: Mapping[str, Any]


class RunsRepo(Protocol):
    async def lock(self, run_id: UUID) -> LockedRun | None:
        """The run with its version's anchor and duration, locked FOR UPDATE until commit."""
        ...

    async def open_lobby(self, run_id: UUID, join_code: str, now: datetime) -> bool:
        """Enter the lobby with this code; False when another live run holds the code."""
        ...

    async def start(self, run: LockedRun, now: datetime, deadline: datetime) -> int:
        """Open the run and give every waiting participant a session and anchor; the count."""
        ...

    async def started_count(self, run_id: UUID) -> int: ...

    async def close(self, run_id: UUID, now: datetime) -> None: ...

    async def warm_input(self, run_id: UUID) -> WarmInput | None: ...
