from dataclasses import dataclass
from datetime import timedelta

from nalar.application.features.scheduler.messages import digest_delivery_message
from nalar.application.ports.clock import Clock
from nalar.application.ports.mailer import Mailer
from nalar.application.ports.parents import DigestItem
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class DigestTiming:
    lease: timedelta
    retry_window: timedelta
    max_attempts: int
    dispatch_ttl: timedelta
    retry_delays: tuple[timedelta, ...]
    daily_limit: int
    batch_size: int
    sender_spacing: timedelta
    quota_cooldown: timedelta
    auth_cooldown: timedelta


def digest_reference(item: DigestItem) -> dict[str, str]:
    return {
        "student_id": str(item.student_id),
        "publication_id": str(item.publication_id),
        "session_id": str(item.session_id),
    }


class WeeklyDigestHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock, mailer: Mailer, timing: DigestTiming) -> None:
        self._uow = uow
        self._clock = clock
        self._mailer = mailer
        self._timing = timing

    async def execute(self) -> int:
        if not self._mailer.enabled:
            return 0
        now = self._clock.now()
        year, week, _ = now.isocalendar()
        async with self._uow:
            for recipient in await self._uow.parents.digest_recipients(now - timedelta(days=7)):
                await self._uow.notifications.queue_digest(
                    recipient.parent_id,
                    recipient.school_id,
                    f"weekly:{recipient.parent_id}:{year}-W{week:02}",
                    [digest_reference(item) for item in recipient.items],
                    now,
                    now + self._timing.retry_window,
                )
        return await self.dispatch_due()

    async def dispatch_due(self) -> int:
        if not self._mailer.enabled:
            return 0
        now = self._clock.now()
        async with self._uow:
            ids = await self._uow.notifications.reserve_due_digests(
                now, now + self._timing.dispatch_ttl, self._timing.batch_size
            )
            for digest_id in ids:
                await self._uow.queue.send(DEFAULT_QUEUE, digest_delivery_message(digest_id))
        return len(ids)
