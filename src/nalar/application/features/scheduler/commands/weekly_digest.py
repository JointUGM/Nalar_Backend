from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from nalar.application.ports.clock import Clock
from nalar.application.ports.mailer import Mailer, MailerError
from nalar.application.ports.notifications import PendingDigest
from nalar.application.ports.parents import DigestItem
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class DigestTiming:
    lease: timedelta
    retry_window: timedelta
    max_attempts: int


def _reference(item: DigestItem) -> dict[str, str]:
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
                    [_reference(item) for item in recipient.items],
                    now,
                )
            pending = await self._uow.notifications.pending_digests(now)
        sent = 0
        retry = False
        for digest in pending:
            delivered, failed = await self._deliver(digest)
            sent += delivered
            retry |= failed
        if retry:
            raise MailerError()
        return sent

    async def _deliver(self, digest: PendingDigest) -> tuple[int, bool]:
        now = self._clock.now()
        token = uuid4()
        async with self._uow:
            recipients = await self._uow.parents.digest_recipients(None, digest.recipient_id)
            recipient = recipients[0] if recipients else None
            items = (
                [item for item in recipient.items if _reference(item) in digest.payload["items"]]
                if recipient
                else []
            )
            delivery: dict[str, Any] = {
                "to": recipient.email if recipient else "",
                "subject": "Ringkasan mingguan NALAR",
                "text": "\n\n".join(
                    f"{item.child_name} — {item.mission_title}\n{item.summary}" for item in items
                ),
                "items": [_reference(item) for item in items],
            }
            claimed = await self._uow.notifications.claim_digest(
                digest.id,
                now,
                now + self._timing.lease,
                token,
                delivery,
            )
            if claimed is None:
                return 0, False
            payload = claimed.payload
            frozen = payload["delivery"]
            expired = (
                now - datetime.fromisoformat(payload["attempted_at"]) >= self._timing.retry_window
            )
            visible = [_reference(item) for item in items]
            if (
                not items
                or frozen["to"] != delivery["to"]
                or any(ref not in visible for ref in frozen["items"])
                or expired
                or payload["attempts"] > self._timing.max_attempts
            ):
                await self._uow.notifications.finish_digest(digest.id, token, "failed", now)
                return 0, False
        try:
            await self._mailer.send(
                frozen["to"],
                frozen["subject"],
                frozen["text"],
                idempotency_key=digest.dedupe_key,
            )
        except MailerError as exc:
            retry = exc.retryable and payload["attempts"] < self._timing.max_attempts
            async with self._uow:
                await self._uow.notifications.finish_digest(
                    digest.id,
                    token,
                    "pending" if retry else "failed",
                    self._clock.now(),
                )
            return 0, retry
        async with self._uow:
            await self._uow.notifications.finish_digest(digest.id, token, "sent", self._clock.now())
        return 1, False
