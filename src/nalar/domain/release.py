from dataclasses import dataclass


@dataclass(frozen=True)
class ReleaseCounts:
    open_runs: int
    active_sessions: int
    unevaluated_sessions: int
    eligible: int
    eligible_without_summary: int
    ineligible: int


@dataclass(frozen=True)
class Readiness:
    ready: bool
    blockers: tuple[tuple[str, int], ...]


def readiness(c: ReleaseCounts) -> Readiness:
    """Every run closed, every started session terminal and evaluated, and at least one
    eligible result, each with its stored summary."""
    blockers = [
        (code, n)
        for code, n in (
            ("RUN_OPEN", c.open_runs),
            ("SESSION_ACTIVE", c.active_sessions),
            ("EVALUATION_PENDING", c.unevaluated_sessions),
            ("SUMMARIES_PENDING", c.eligible_without_summary),
        )
        if n > 0
    ]
    if c.eligible == 0:
        blockers.append(("NO_ELIGIBLE_RESULT", 0))
    return Readiness(not blockers, tuple(blockers))
