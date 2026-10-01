from datetime import timedelta

from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.release import readiness

_READY_FOR = timedelta(hours=24)


class ReleaseRemindersHandler:
    """D-S26-15: one in-app reminder per teacher, 24 h after the finalizer made it ready."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self) -> int:
        sent = 0
        async with self._uow:
            for publication_id, school_id in await self._uow.release.reminder_candidates(
                self._clock.now() - _READY_FOR
            ):
                if not readiness(await self._uow.release.counts(publication_id)).ready:
                    continue
                teachers = await self._uow.publications.teacher_ids(publication_id)
                sent += await self._uow.notifications.release_reminder(
                    teachers, school_id, publication_id
                )
        return sent
