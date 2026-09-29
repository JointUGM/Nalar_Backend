from datetime import datetime
from typing import Protocol
from uuid import UUID


class SchedulerRepo(Protocol):
    """Each method is one guarded statement, safe to run twice (NFR-R3)."""

    async def open_due_windows(self, now: datetime) -> list[UUID]: ...

    async def close_due_windows(self, now: datetime) -> list[UUID]: ...

    async def time_out_overdue(self, now: datetime) -> list[UUID]: ...

    async def stuck_turns(self, answered_before: datetime) -> list[tuple[UUID, int]]:
        """Answered open turns of in-progress sessions that no turn step has advanced."""
        ...

    async def unevaluated_sessions(self, ended_before: datetime) -> list[UUID]:
        """Ended sessions with no evaluation and no queued or archived evaluation message."""
        ...
