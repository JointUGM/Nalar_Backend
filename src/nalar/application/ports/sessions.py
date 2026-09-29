from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from nalar.domain.labels import RunMode, RunStatus, SessionStatus


@dataclass(frozen=True)
class SessionRef:
    id: UUID
    run_id: UUID
    status: SessionStatus
    started_at: datetime
    deadline_at: datetime


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


class SessionsRepo(Protocol):
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
        """Primary runs of publications in the student's active classes, newest first."""
        ...
