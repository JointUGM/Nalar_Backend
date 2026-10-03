import asyncio
import logging
from datetime import timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

from nalar.application.features.auth.commands.request_password_reset import PasswordResetPolicy
from nalar.application.features.onboarding.commands.send_invitation import InvitationTiming
from nalar.application.ports.auth_admin import AuthAdmin, AuthEmailError
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.uow import UnitOfWork

log = logging.getLogger(__name__)


class SendPasswordResetHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        admin: AuthAdmin,
        timing: InvitationTiming,
        policy: PasswordResetPolicy,
    ) -> None:
        self._uow, self._clock, self._admin, self._timing, self._policy = (
            uow,
            clock,
            admin,
            timing,
            policy,
        )

    async def dispatch_due(self) -> int:
        now = self._clock.now()
        async with self._uow:
            ids = await self._uow.password_resets.reserve_due(
                now,
                now + self._timing.dispatch_ttl,
                self._timing.batch_size if self._policy.enabled else 0,
            )
            for reset_id in ids:
                await self._uow.queue.send(
                    DEFAULT_QUEUE, {"kind": "send_password_reset", "reset_id": str(reset_id)}
                )
        return len(ids)

    async def execute(self, reset_id: UUID) -> None:
        if not self._policy.enabled:
            return
        token, now = uuid4(), self._clock.now()
        async with self._uow:
            reset = await self._uow.password_resets.claim_delivery(
                reset_id,
                token,
                now,
                now + self._timing.lease,
                self._timing.sender_key,
                self._timing.hourly_limit,
                self._timing.daily_limit,
                self._timing.sender_spacing,
                self._timing.max_attempts,
            )
            if reset is None:
                return
            recipient = await self._uow.password_resets.recipient(reset.user_id, recovery=True)
            marked = (
                recipient is not None
                and recipient.email.lower() == reset.email.lower()
                and (
                    await self._uow.password_resets.mark_submitting(
                        reset, token, now, self._timing.link_lifetime, self._timing.sender_key
                    )
                )
            )
            if not marked:
                await self._uow.password_resets.finish_delivery(
                    reset_id, token, now, accepted=False, category="ineligible"
                )
                return
        try:
            assert self._policy.redirect_url is not None
            async with asyncio.timeout(self._timing.total_timeout_s):
                await self._admin.send_setup_email(
                    reset.email,
                    self._policy.redirect_url + "?" + urlencode({"reset_id": str(reset.id)}),
                )
        except (AuthEmailError, TimeoutError) as exc:
            error = (
                exc
                if isinstance(exc, AuthEmailError)
                else AuthEmailError("deadline", acceptance_unknown=True)
            )
            retry_at = None
            suspended = error.category == "auth"
            if (
                (error.retryable or suspended)
                and not error.acceptance_unknown
                and reset.attempts < self._timing.max_attempts
            ):
                delay = self._timing.retry_delays[reset.attempts - 1]
                if error.retry_after_s is not None:
                    try:
                        delay = max(delay, timedelta(seconds=error.retry_after_s))
                    except OverflowError:
                        delay = reset.queue_expires_at - self._clock.now()
                if suspended:
                    delay = max(delay, self._timing.auth_cooldown)
                proposed = self._clock.now() + delay
                if proposed < reset.queue_expires_at:
                    retry_at = proposed
            async with self._uow:
                await self._uow.password_resets.finish_delivery(
                    reset_id,
                    token,
                    self._clock.now(),
                    accepted=False,
                    category=error.category,
                    retry_at=retry_at,
                    suspended=suspended,
                )
            log.warning(
                "password reset delivery failed",
                extra={"reset_id": str(reset_id), "category": error.category},
            )
            return
        async with self._uow:
            await self._uow.password_resets.finish_delivery(
                reset_id, token, self._clock.now(), accepted=True
            )
