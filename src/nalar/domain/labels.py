from collections.abc import Mapping
from enum import StrEnum
from typing import Final


class RunStatus(StrEnum):
    scheduled = "scheduled"
    lobby = "lobby"
    open = "open"
    closed = "closed"


class RunMode(StrEnum):
    window = "window"
    live = "live"


class SessionStatus(StrEnum):
    in_progress = "in_progress"
    paused_safety = "paused_safety"
    completed = "completed"
    timed_out = "timed_out"
    ended_safety = "ended_safety"


class SessionEndReason(StrEnum):
    student_completed = "student_completed"
    max_turns_reached = "max_turns_reached"
    max_duration_reached = "max_duration_reached"
    run_closed_grace_expired = "run_closed_grace_expired"
    safety_pause = "safety_pause"


class ParticipantStatus(StrEnum):
    waiting = "waiting"
    started = "started"
    cancelled = "cancelled"


class EvaluationStatus(StrEnum):
    completed = "completed"
    no_answer = "no_answer"
    failed = "failed"


PROBE_MOVES: Final[tuple[str, ...]] = (
    "request_justification",
    "counter_example",
    "transfer",
    "decompose",
    "refuse_and_redirect",
    "deeper_reason",
    "explain_mechanism",
    "simpler_reason",
)

ANSWER_STATE_BY_AI_TYPE: Final[Mapping[str, str | None]] = {
    "correct_reasoned": "correct_reasoned",
    "correct_unreasoned": "correct_unreasoned",
    "misconception": "misconception",
    "mixed": "mixed",
    "evasive": "evasive",
    "manipulation": "manipulation_attempt",
    "unsure": None,
    "safety": None,
}

AI_TYPE_BY_ANSWER_STATE: Final[Mapping[str, str]] = {
    state: ai_type for ai_type, state in ANSWER_STATE_BY_AI_TYPE.items() if state is not None
}

SESSION_END_BY_AI_REASON: Final[Mapping[str, tuple[SessionStatus, SessionEndReason]]] = {
    "coverage_complete": (SessionStatus.completed, SessionEndReason.student_completed),
    "turn_limit": (SessionStatus.completed, SessionEndReason.max_turns_reached),
    "time_limit": (SessionStatus.timed_out, SessionEndReason.max_duration_reached),
}
