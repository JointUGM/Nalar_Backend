import pytest

from nalar.domain.release import ReleaseCounts, readiness

READY = ReleaseCounts(
    open_runs=0,
    active_sessions=0,
    unevaluated_sessions=0,
    eligible=3,
    eligible_without_summary=0,
    ineligible=1,
)


def test_ready_when_nothing_blocks() -> None:
    assert readiness(READY).ready
    assert readiness(READY).blockers == ()


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("open_runs", "RUN_OPEN"),
        ("active_sessions", "SESSION_ACTIVE"),
        ("unevaluated_sessions", "EVALUATION_PENDING"),
        ("eligible_without_summary", "SUMMARIES_PENDING"),
    ],
)
def test_each_blocker_carries_its_count(field: str, code: str) -> None:
    result = readiness(ReleaseCounts(**{**vars(READY), field: 2}))
    assert not result.ready
    assert result.blockers == ((code, 2),)


def test_no_eligible_result_blocks() -> None:
    result = readiness(ReleaseCounts(**{**vars(READY), "eligible": 0}))
    assert result.blockers == (("NO_ELIGIBLE_RESULT", 0),)
