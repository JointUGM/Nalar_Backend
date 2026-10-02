import hashlib
import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from nalar.application.features.scheduler.commands.weekly_digest import (
    DigestTiming,
    digest_reference,
)
from nalar.application.ports.clock import Clock
from nalar.application.ports.mailer import Mailer, MailerError, MailSubmission
from nalar.application.ports.notifications import PendingDigest
from nalar.application.ports.uow import UnitOfWork

log = logging.getLogger(__name__)


class DeliverDigestHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock, mailer: Mailer, timing: DigestTiming) -> None:
        self._uow = uow
        self._clock = clock
        self._mailer = mailer
        self._timing = timing

    async def _delivery(
        self, digest: PendingDigest, *, for_submission: bool = False
    ) -> dict[str, Any]:
        recipients = await self._uow.parents.digest_recipients(
            None, digest.recipient_id, for_submission=for_submission
        )
        recipient = recipients[0] if recipients else None
        items = (
            [item for item in recipient.items if digest_reference(item) in digest.payload["items"]]
            if recipient
            else []
        )
        return {
            "to": recipient.email if recipient else "",
            "subject": "Ringkasan mingguan NALAR",
            "text": "\n\n".join(
                f"{item.child_name} — {item.mission_title}\n{item.summary}" for item in items
            ),
            "items": [digest_reference(item) for item in items],
        }

    @staticmethod
    def _visible(frozen: dict[str, Any], current: dict[str, Any]) -> bool:
        return (
            bool(frozen["items"])
            and frozen["to"] == current["to"]
            and all(ref in current["items"] for ref in frozen["items"])
        )

    async def execute(self, digest_id: UUID) -> None:
        sender_key = self._mailer.sender_key
        if not self._mailer.enabled or sender_key is None:
            return
        now, token = self._clock.now(), uuid4()
        async with self._uow:
            digest = await self._uow.notifications.get_digest(digest_id)
            if digest is None:
                return
            current = await self._delivery(digest)
            identifier = hashlib.sha256(f"{sender_key}:{digest.dedupe_key}".encode()).hexdigest()
            current["message_id"] = f"<{identifier}@gmail.com>"
            claimed = await self._uow.notifications.claim_digest(
                digest.id,
                now,
                now + self._timing.lease,
                token,
                current,
                sender_key=sender_key,
                daily_limit=self._timing.daily_limit,
                sender_spacing=self._timing.sender_spacing,
            )
            if claimed is None:
                return
            frozen = claimed.payload["delivery"]
            if (
                not self._visible(frozen, current)
                or claimed.payload["attempts"] > self._timing.max_attempts
            ):
                await self._uow.notifications.finish_digest(
                    digest.id, token, "failed", now, outcome="skipped", category="ineligible"
                )
                return
        prepared_at = datetime.fromisoformat(claimed.payload["prepared_at"])
        submission = MailSubmission(
            frozen["to"],
            frozen["subject"],
            frozen["text"],
            frozen["message_id"],
            prepared_at,
        )

        async def before_submit() -> bool:
            async with self._uow:
                visible = await self._delivery(digest, for_submission=True)
                if not self._visible(frozen, visible):
                    await self._uow.notifications.finish_digest(
                        digest.id,
                        token,
                        "failed",
                        self._clock.now(),
                        outcome="skipped",
                        category="visibility_changed",
                    )
                    return False
                return await self._uow.notifications.mark_digest_submitting(
                    digest.id, token, self._clock.now(), sender_key
                )

        try:
            accepted = await self._mailer.send(submission, before_submit=before_submit)
        except MailerError as exc:
            now = self._clock.now()
            attempts = int(claimed.payload["attempts"])
            hold = None
            retry_at = now
            if attempts < self._timing.max_attempts:
                retry_at += self._timing.retry_delays[attempts - 1]
            if exc.sender_action == "cooldown":
                cooldown = (
                    self._timing.quota_cooldown
                    if exc.category == "quota"
                    else self._timing.auth_cooldown
                )
                hold = now + cooldown
                retry_at = max(retry_at, hold)
            retry = (
                not exc.acceptance_unknown
                and (exc.retryable or exc.sender_action != "none")
                and attempts < self._timing.max_attempts
                and retry_at < datetime.fromisoformat(claimed.payload["expires_at"])
            )
            async with self._uow:
                await self._uow.notifications.finish_digest(
                    digest.id,
                    token,
                    "pending" if retry else "failed",
                    now,
                    outcome="unknown" if exc.acceptance_unknown else "rejected",
                    category=exc.category,
                    retry_at=retry_at if retry else None,
                    sender_hold_until=hold,
                    sender_suspended=exc.sender_action == "suspend",
                    smtp_code=exc.smtp_code,
                    enhanced_code=exc.enhanced_code,
                )
            log.warning(
                "email submission failed",
                extra={
                    "notification_id": str(digest.id),
                    "category": exc.category,
                    "smtp_code": exc.smtp_code,
                    "enhanced_code": exc.enhanced_code,
                },
            )
            return
        async with self._uow:
            await self._uow.notifications.finish_digest(
                digest.id,
                token,
                "sent" if accepted else "failed",
                self._clock.now(),
                outcome="accepted" if accepted else "skipped",
                category=None if accepted else "claim_lost",
                smtp_code=250 if accepted else None,
            )
