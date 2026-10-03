from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from nalar.application.errors import NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.results import ChangedMisconception, DashboardBucket
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ReportingTimezone:
    name: str


@dataclass(frozen=True)
class DashboardWeek:
    week_start: datetime
    week_end: datetime
    sessions_completed: int
    students: int
    active_misconceptions: int
    concepts_with_misconceptions: int
    changed_mind_rate: float | None
    open_flags: int


@dataclass(frozen=True)
class DashboardTrend:
    week_start: datetime
    mastered: int
    developing: int
    misconception: int


@dataclass(frozen=True)
class TeacherDashboard:
    this_week: DashboardWeek
    last_week: DashboardWeek
    trend: list[DashboardTrend]
    top_changed: list[ChangedMisconception]
    as_of: datetime
    timezone: str


class TeacherDashboardQuery:
    def __init__(self, uow: UnitOfWork, clock: Clock, timezone: ReportingTimezone) -> None:
        self._uow, self._clock, self._timezone = uow, clock, timezone

    async def execute(self, actor_id: UUID, school_id: UUID) -> TeacherDashboard:
        async with self._uow:
            if not await self._uow.authz.is_school_teacher(actor_id, school_id):
                raise NotFound()
            now = self._clock.now()
            zone = ZoneInfo(self._timezone.name)
            local_now = now.astimezone(zone)
            monday = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
                days=local_now.weekday()
            )
            starts = [monday - timedelta(weeks=i) for i in (3, 2, 1, 0)]
            ends = starts[1:] + [local_now]
            rows, changed = await self._uow.results.teacher_dashboard(
                actor_id,
                school_id,
                [
                    (start.astimezone(UTC), end.astimezone(UTC))
                    for start, end in zip(starts, ends, strict=True)
                ],
            )

        def week(index: int, row: DashboardBucket) -> DashboardWeek:
            return DashboardWeek(
                starts[index],
                ends[index],
                row.sessions_completed,
                row.students,
                row.active_misconceptions,
                row.concepts_with_misconceptions,
                row.changed_mind_rate,
                row.open_flags,
            )

        return TeacherDashboard(
            week(3, rows[3]),
            week(2, rows[2]),
            [
                DashboardTrend(start, row.mastered, row.developing, row.misconception)
                for start, row in zip(starts, rows, strict=True)
            ],
            changed,
            now,
            zone.key,
        )
