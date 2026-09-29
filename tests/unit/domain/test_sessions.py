from datetime import UTC, datetime

import pytest

from nalar.domain.labels import SessionStatus
from nalar.domain.sessions import attempt_status, deadline_at


def test_deadline_is_started_at_plus_the_version_duration_in_utc() -> None:
    started = datetime(2026, 10, 8, 1, 50, tzinfo=UTC)
    assert deadline_at(started, 20) == datetime(2026, 10, 8, 2, 10, tzinfo=UTC)


def test_deadline_rejects_a_naive_start() -> None:
    with pytest.raises(ValueError):
        deadline_at(datetime(2026, 10, 8, 1, 50), 20)


@pytest.mark.parametrize(
    ("latest", "expected"),
    [
        (None, "not_started"),
        (SessionStatus.in_progress, "in_progress"),
        (SessionStatus.paused_safety, "in_progress"),
        (SessionStatus.completed, "completed"),
        (SessionStatus.timed_out, "incomplete"),
        (SessionStatus.ended_safety, "incomplete"),
    ],
)
def test_attempt_status_for_the_mission_card(latest: SessionStatus | None, expected: str) -> None:
    assert attempt_status(latest) == expected
