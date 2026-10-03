from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from nalar.domain.labels import RunMode, RunStatus, SessionEndReason, SessionStatus


@dataclass(frozen=True)
class SessionRef:
    id: UUID
    run_id: UUID
    status: SessionStatus
    started_at: datetime
    deadline_at: datetime


@dataclass(frozen=True)
class StateView:
    status: SessionStatus
    started_at: datetime
    deadline_at: datetime
    max_turns: int
    latest_turn_index: int
    latest_kind: str
    latest_text: str
    latest_answered_at: datetime | None
    evaluated: bool
    reflection_ready: bool


@dataclass(frozen=True)
class StoredTurn:
    id: UUID
    turn_index: int
    kind: str
    question_text: str
    answer_text: str | None
    answer_state: str | None
    move: str | None
    target_concept_id: UUID | None
    question_bank_id: str | None
    detected_misconception_id: UUID | None
    secondary_misconception_id: UUID | None


@dataclass(frozen=True)
class TurnContext:
    session_id: UUID
    school_id: UUID
    publication_id: UUID
    status: SessionStatus
    started_at: datetime
    deadline_at: datetime
    max_turns: int
    context_pack: Mapping[str, Any]
    planner_mode: str
    turns: tuple[StoredTurn, ...]


@dataclass(frozen=True)
class StudentMissionRow:
    publication_id: UUID
    mission_title: str
    subject_name: str
    mode: RunMode
    opens_at: datetime | None
    closes_at: datetime | None
    run_status: RunStatus
    latest_status: SessionStatus | None
    max_duration_minutes: int
    session_id: UUID | None = None
    run_id: UUID | None = None
    attempt_number: int = 1
    is_granted_attempt: bool = False


@dataclass(frozen=True)
class StudentReflection:
    session_id: UUID
    mission_title: str
    completed_at: datetime
    content: str


class SessionsRepo(Protocol):
    async def next_attempt_number(self, publication_id: UUID, student_id: UUID) -> int:
        """Allocate while the caller holds the publication admission lock."""
        ...

    async def student_reflections(
        self, student_id: UUID, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[StudentReflection]:
        """Stored terminal reflections scoped to the caller's active student memberships."""
        ...

    async def create_with_anchor(
        self,
        *,
        school_id: UUID,
        publication_id: UUID,
        run_id: UUID,
        student_id: UUID,
        attempt_number: int,
        started_at: datetime,
        deadline_at: datetime,
        anchor_text: str,
    ) -> UUID | None:
        """None when this attempt number already exists (a concurrent duplicate)."""
        ...

    async def latest_for_student(
        self, publication_id: UUID, student_id: UUID
    ) -> SessionRef | None: ...

    async def for_run(self, run_id: UUID, student_id: UUID) -> SessionRef | None: ...

    async def get_ref(self, session_id: UUID) -> SessionRef | None: ...

    async def student_mission_rows(self, student_id: UUID) -> list[StudentMissionRow]:
        """Primary and personally granted runs in the student's active classes, newest first."""
        ...

    async def touch(self, session_id: UUID, now: datetime) -> None: ...

    async def time_out(self, session_id: UUID, now: datetime) -> bool:
        """Open and past its deadline → timed_out with ended_at = deadline_at; True only once."""
        ...

    async def state_view(self, session_id: UUID) -> StateView | None: ...

    async def turn_context(self, session_id: UUID) -> TurnContext | None: ...

    async def lock_if_in_progress(self, session_id: UUID) -> bool:
        """Lock the session row FOR UPDATE until commit; True when it is still in progress."""
        ...

    async def end(
        self, session_id: UUID, status: SessionStatus, reason: SessionEndReason, now: datetime
    ) -> bool:
        """In progress → a terminal status, once; True for the caller that did it."""
        ...

    async def pause_for_safety(self, session_id: UUID) -> bool: ...

    async def resume(self, session_id: UUID, now: datetime) -> bool:
        """paused_safety → in_progress while before the deadline; holds the row lock."""
        ...

    async def end_safety(self, session_id: UUID, now: datetime) -> bool:
        """paused_safety → ended_safety, once."""
        ...
