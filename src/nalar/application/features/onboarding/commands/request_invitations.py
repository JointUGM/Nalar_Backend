import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from nalar.application.errors import DependencyUnavailable, InvalidInput, NotFound
from nalar.application.ports.activations import InvitationAdmission, InvitationRecipient
from nalar.application.ports.auth_admin import AuthAccount, AuthAdmin, AuthAdminError
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class InvitationRequestLimits:
    queue_ttl: timedelta
    cooldown: timedelta
    lookup_timeout_s: float
    lookup_concurrency: int


class RequestInvitationsHandler:
    def __init__(
        self, uow: UnitOfWork, clock: Clock, admin: AuthAdmin, limits: InvitationRequestLimits
    ) -> None:
        self._uow, self._clock, self._admin, self._limits = uow, clock, admin, limits

    async def execute(
        self, actor_id: UUID, school_id: UUID, user_ids: list[UUID], resend: bool
    ) -> list[InvitationAdmission]:
        ids = sorted(set(user_ids))
        async with self._uow:
            if not await self._uow.authz.is_school_admin(actor_id, school_id):
                raise NotFound()
            if not 1 <= len(user_ids) <= 100:
                raise InvalidInput()
            recipients = await self._recipients(school_id, ids)
        accounts = await self._accounts(recipients)
        now = self._clock.now()
        results = []
        async with self._uow:
            if not await self._uow.authz.is_school_admin(actor_id, school_id):
                raise NotFound()
            recipients = await self._recipients(school_id, ids, lock=True)
            for recipient in recipients:
                reason = None
                if (
                    not recipient.has_real_email
                    or not recipient.email
                    or not recipient.email.strip()
                ):
                    reason = "requires_assistance"
                elif not recipient.onboarding_required:
                    account = accounts.get(recipient.user_id)
                    if (
                        not account
                        or account.id != recipient.user_id
                        or not account.email
                        or (account.email.lower() != recipient.email.lower())
                    ):
                        reason = "identity_changed"
                    elif (
                        account.last_sign_in_at is not None
                        or not await self._uow.activations.enable_onboarding(
                            recipient.user_id, recipient.email
                        )
                    ):
                        reason = "already_active"
                if reason:
                    results.append(InvitationAdmission(recipient.user_id, None, False, reason))
                    continue
                admitted = await self._uow.activations.request_invitation(
                    recipient.user_id,
                    school_id,
                    actor_id,
                    now,
                    now + self._limits.queue_ttl,
                    resend=resend,
                    cooldown=self._limits.cooldown,
                )
                results.append(admitted)
                if admitted.queued:
                    await self._uow.audit.record(
                        school_id,
                        actor_id,
                        "account_invitation.request",
                        "profiles",
                        recipient.user_id,
                        {
                            "notification_id": str(admitted.notification_id),
                            "resend": resend,
                            "reason": admitted.reason,
                        },
                    )
        return results

    async def _recipients(
        self, school_id: UUID, ids: list[UUID], *, lock: bool = False
    ) -> list[InvitationRecipient]:
        recipients = await self._uow.activations.request_recipients(school_id, ids, lock=lock)
        if len(recipients) != len(ids):
            raise NotFound()
        return recipients

    async def _accounts(
        self, recipients: list[InvitationRecipient]
    ) -> dict[UUID, AuthAccount | None]:
        semaphore = asyncio.Semaphore(self._limits.lookup_concurrency)

        async def lookup(recipient: InvitationRecipient) -> tuple[UUID, AuthAccount | None]:
            async with semaphore:
                return recipient.user_id, await self._admin.get_account(recipient.user_id)

        tasks = []
        try:
            async with asyncio.timeout(self._limits.lookup_timeout_s):
                for recipient in recipients:
                    if (
                        not recipient.onboarding_required
                        and recipient.has_real_email
                        and recipient.email
                        and recipient.email.strip()
                    ):
                        tasks.append(asyncio.create_task(lookup(recipient)))
                return dict(await asyncio.gather(*tasks))
        except (AuthAdminError, TimeoutError) as exc:
            raise DependencyUnavailable() from exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
