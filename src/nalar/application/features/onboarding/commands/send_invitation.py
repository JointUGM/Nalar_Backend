import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

from nalar.application.features.onboarding.messages import invitation_message
from nalar.application.ports.activations import PendingInvitation
from nalar.application.ports.auth_admin import AuthAdmin, AuthAdminError, AuthEmailError
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.uow import UnitOfWork

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvitationTiming:
    enabled: bool
    activation_url: str | None
    sender_key: str
    total_timeout_s: float
    lease: timedelta
    dispatch_ttl: timedelta
    link_lifetime: timedelta
    max_attempts: int
    retry_delays: tuple[timedelta, ...]
    hourly_limit: int
    daily_limit: int
    batch_size: int
    sender_spacing: timedelta
    auth_cooldown: timedelta


class SendAccountInvitationHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        admin: AuthAdmin,
        timing: InvitationTiming,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._admin = admin
        self._timing = timing

    async def dispatch_due(self) -> int:
        now = self._clock.now()
        async with self._uow:
            ids = await self._uow.activations.reserve_due(
                now,
                now + self._timing.dispatch_ttl,
                self._timing.batch_size if self._timing.enabled else 0,
            )
            for notification_id in ids:
                await self._uow.queue.send(DEFAULT_QUEUE, invitation_message(notification_id))
        return len(ids)

    async def execute(self, notification_id: UUID) -> None:
        if not self._timing.enabled:
            return
        token, now = uuid4(), self._clock.now()
        async with self._uow:
            pending = await self._uow.activations.get_invitation(notification_id)
            if pending is None:
                return
            eligible = await self._eligible(pending)
            invitation = await self._uow.activations.claim(
                notification_id,
                token,
                now,
                now + self._timing.lease,
                sender_key=self._timing.sender_key,
                hourly_limit=self._timing.hourly_limit,
                daily_limit=self._timing.daily_limit,
                sender_spacing=self._timing.sender_spacing,
                max_attempts=self._timing.max_attempts,
            )
            if invitation is None:
                return
            if not eligible:
                await self._uow.activations.finish(
                    notification_id,
                    token,
                    "failed",
                    now,
                    outcome="skipped",
                    category="ineligible",
                )
                return
        submitting = False
        try:
            async with asyncio.timeout(self._timing.total_timeout_s):
                account = await self._admin.get_account(invitation.user_id)
                if (
                    account is None
                    or account.id != invitation.user_id
                    or account.email is None
                    or account.email.lower() != invitation.email.lower()
                ):
                    async with self._uow:
                        await self._uow.activations.finish(
                            notification_id,
                            token,
                            "failed",
                            self._clock.now(),
                            outcome="skipped",
                            category="auth_identity_changed",
                        )
                    return
                async with self._uow:
                    if not await self._eligible(invitation):
                        await self._uow.activations.finish(
                            notification_id,
                            token,
                            "failed",
                            self._clock.now(),
                            outcome="skipped",
                            category="eligibility_changed",
                        )
                        return
                    submitting = True
                    marked = await self._uow.activations.mark_submitting(
                        notification_id,
                        token,
                        self._clock.now(),
                        self._timing.link_lifetime,
                        self._timing.sender_key,
                    )
                    if not marked:
                        return
                assert self._timing.activation_url is not None
                redirect = (
                    self._timing.activation_url
                    + "?"
                    + urlencode({"activation_id": str(invitation.activation_id)})
                )
                await self._admin.send_setup_email(invitation.email, redirect)
        except AuthAdminError as exc:
            await self._failed(
                invitation,
                token,
                AuthEmailError(
                    "lookup" if exc.retryable else "auth",
                    retryable=exc.retryable,
                ),
            )
            return
        except AuthEmailError as exc:
            await self._failed(invitation, token, exc)
            return
        except TimeoutError:
            await self._failed(
                invitation,
                token,
                AuthEmailError(
                    "deadline",
                    retryable=not submitting,
                    acceptance_unknown=submitting,
                ),
            )
            return
        async with self._uow:
            bound = await self._uow.activations.bind_proof(notification_id, token)
            await self._uow.activations.finish(
                notification_id,
                token,
                "sent" if bound else "failed",
                self._clock.now(),
                outcome="accepted" if bound else "unknown",
                category=None if bound else "proof_binding",
            )

    async def _eligible(self, invitation: PendingInvitation) -> bool:
        if invitation.issuer_id is None:
            return False
        issuer_allowed = await self._uow.authz.is_school_admin(
            invitation.issuer_id, invitation.school_id
        ) or await self._uow.authz.is_platform_admin(invitation.issuer_id)
        return issuer_allowed and await self._uow.activations.eligible(invitation)

    async def _failed(
        self,
        invitation: PendingInvitation,
        token: UUID,
        error: AuthEmailError,
    ) -> None:
        now = self._clock.now()
        retry_at = now
        if invitation.attempts < self._timing.max_attempts:
            retry_at += self._timing.retry_delays[invitation.attempts - 1]
        if error.retry_after_s is not None:
            try:
                retry_at = max(retry_at, now + timedelta(seconds=error.retry_after_s))
            except OverflowError:
                retry_at = datetime.max.replace(tzinfo=now.tzinfo)
        suspended = error.category == "auth"
        if suspended:
            retry_at = max(retry_at, now + self._timing.auth_cooldown)
        retry = (
            not error.acceptance_unknown
            and (error.retryable or suspended)
            and (invitation.attempts < self._timing.max_attempts or suspended)
            and retry_at < invitation.queue_expires_at
        )
        async with self._uow:
            await self._uow.activations.finish(
                invitation.id,
                token,
                "pending" if retry else "failed",
                now,
                outcome="unknown" if error.acceptance_unknown else "rejected",
                category=error.category,
                retry_at=retry_at if retry else None,
                sender_hold_until=retry_at if error.category == "rate_limited" else None,
                sender_suspended=suspended,
            )
        log.warning(
            "account email submission failed",
            extra={
                "notification_id": str(invitation.id),
                "category": error.category,
            },
        )
