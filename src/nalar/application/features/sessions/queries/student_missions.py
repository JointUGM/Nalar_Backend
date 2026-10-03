from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import RunMode, RunStatus
from nalar.domain.sessions import attempt_status

type Bucket = Literal["upcoming", "open", "completed"]


@dataclass(frozen=True)
class MissionCard:
    publication_id: UUID
    mission_title: str
    subject_name: str
    mode: RunMode
    opens_at: datetime | None
    closes_at: datetime | None
    run_status: RunStatus
    attempt_status: str
    max_duration_minutes: int
    session_id: UUID | None = None

    @property
    def bucket(self) -> Bucket:
        if (
            self.attempt_status in ("completed", "incomplete")
            or self.run_status is RunStatus.closed
        ):
            return "completed"
        return "upcoming" if self.run_status is RunStatus.scheduled else "open"


class StudentMissionsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID) -> list[MissionCard]:
        async with self._uow:
            # The SQL is scoped to the caller's active enrollments; that is its authz.
            rows = await self._uow.sessions.student_mission_rows(actor_id)
        return [
            MissionCard(
                publication_id=r.publication_id,
                session_id=r.session_id,
                mission_title=r.mission_title,
                subject_name=r.subject_name,
                mode=r.mode,
                opens_at=r.opens_at,
                closes_at=r.closes_at,
                run_status=r.run_status,
                attempt_status=attempt_status(r.latest_status),
                max_duration_minutes=r.max_duration_minutes,
            )
            for r in rows
        ]
