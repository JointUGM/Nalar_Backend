from datetime import datetime, timedelta
from typing import Final

from nalar.domain.labels import SessionStatus

OPEN_STATUSES: Final = frozenset({SessionStatus.in_progress, SessionStatus.paused_safety})
TERMINAL_STATUSES: Final = frozenset(
    {SessionStatus.completed, SessionStatus.timed_out, SessionStatus.ended_safety}
)
INCOMPLETE_STATUSES: Final = frozenset({SessionStatus.timed_out, SessionStatus.ended_safety})

# Shown instead of any model text while a session is paused for safety (S3 safety rule).
SAFETY_MESSAGE: Final = (
    "Terima kasih sudah jujur bercerita. Kamu tidak sendirian. "
    "Gurumu sudah diberi tahu dan akan segera menemuimu."
)


def deadline_at(started_at: datetime, max_duration_minutes: int) -> datetime:
    if started_at.tzinfo is None:
        raise ValueError("started_at must be timezone-aware")
    return started_at + timedelta(minutes=max_duration_minutes)


def attempt_status(latest: SessionStatus | None) -> str:
    if latest is None:
        return "not_started"
    if latest in OPEN_STATUSES:
        return "in_progress"
    if latest is SessionStatus.completed:
        return "completed"
    return "incomplete"
