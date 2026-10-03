from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from nalar.domain.labels import ParticipantStatus, RunMode, RunStatus


@dataclass(frozen=True)
class JoinTarget:
    run_id: UUID
    school_id: UUID
    publication_id: UUID
    class_id: UUID
    mode: RunMode
    status: RunStatus
    mission_title: str
    anchor_problem: str
    max_duration_minutes: int
    live_warmup: Mapping[str, Any] | None
    kind: str = "primary"
    grant_student_id: UUID | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None


@dataclass(frozen=True)
class Participant:
    id: UUID
    run_id: UUID
    student_id: UUID
    status: ParticipantStatus
    warmup_choice_id: str | None
    warmup_submitted_at: datetime | None
    session_id: UUID | None


class ParticipantsRepo(Protocol):
    async def joinable_by_code(self, code: str) -> JoinTarget | None:
        """The lobby or open run with this code, locked FOR SHARE so Start can't race the join."""
        ...

    async def latest_closed_by_code(self, code: str) -> JoinTarget | None: ...

    async def run_target(self, run_id: UUID) -> JoinTarget | None: ...

    async def window_target(
        self, publication_id: UUID, run_id: UUID | None = None, lock: bool = False
    ) -> JoinTarget | None:
        """The requested run, optionally locked after the publication admission lock."""
        ...

    async def get(self, run_id: UUID, student_id: UUID) -> Participant | None: ...

    async def insert_waiting(
        self, school_id: UUID, run_id: UUID, student_id: UUID, now: datetime
    ) -> Participant:
        """Idempotent on (run, student): a concurrent duplicate returns the existing row."""
        ...

    async def insert_started(
        self, school_id: UUID, run_id: UUID, student_id: UUID, session_id: UUID, now: datetime
    ) -> Participant: ...

    async def submit_warmup(self, participant_id: UUID, choice_id: str, now: datetime) -> bool:
        """False when a choice is already stored or the participant is no longer waiting."""
        ...
