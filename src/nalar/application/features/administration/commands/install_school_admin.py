import asyncio
from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import DependencyUnavailable, NotFound
from nalar.application.features.administration.commands.request_digest import request_digest
from nalar.application.features.onboarding.commands.request_invitations import (
    InvitationRequestLimits,
)
from nalar.application.ports.administration import NewSchool
from nalar.application.ports.auth_admin import AuthAdmin, AuthAdminError
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class AdminInstallation:
    result_id: UUID
    pending_activation: bool


class InstallSchoolAdminHandler:
    def __init__(
        self, uow: UnitOfWork, clock: Clock, admin: AuthAdmin, limits: InvitationRequestLimits
    ) -> None:
        self._uow, self._clock, self._admin, self._limits = uow, clock, admin, limits

    async def execute(
        self, actor_id: UUID, key: UUID, details: NewSchool | str, school_id: UUID | None = None
    ) -> AdminInstallation:
        operation = "school_admin" if school_id else "school_onboard"
        scope = school_id or actor_id
        digest = request_digest(details)
        async with self._uow:
            if not await self._uow.authz.is_platform_admin(actor_id):
                raise NotFound()
            if school_id and not await self._uow.administration.lock_school(school_id):
                raise NotFound()
            _, previous = await self._uow.administration.request(
                actor_id, operation, scope, key, digest
            )
            if previous:
                return AdminInstallation(
                    previous,
                    await self._uow.administration.pending_admin(school_id or previous) is not None,
                )
            email = details.admin_email if isinstance(details, NewSchool) else details
            existing = await self._uow.roster.profile_by_email(email)
        try:
            async with asyncio.timeout(self._limits.lookup_timeout_s):
                account = (
                    await self._admin.get_account(existing)
                    if existing
                    else await self._admin.create_or_find(email, email.split("@")[0])
                )
        except (AuthAdminError, TimeoutError) as exc:
            raise DependencyUnavailable() from exc
        if account is None or account.email is None or account.email.lower() != email.lower():
            raise DependencyUnavailable()
        async with self._uow:
            if not await self._uow.authz.is_platform_admin(actor_id):
                raise NotFound()
            if school_id and not await self._uow.administration.lock_school(school_id):
                raise NotFound()
            request_id, previous = await self._uow.administration.request(
                actor_id, operation, scope, key, digest
            )
            if previous:
                return AdminInstallation(
                    previous,
                    await self._uow.administration.pending_admin(school_id or previous) is not None,
                )
            await self._uow.roster.ensure_profile(
                account.id,
                email.split("@")[0],
                email,
                True,
                onboarding_required=account.last_sign_in_at is None,
            )
            if school_id is None:
                assert isinstance(details, NewSchool)
                school_id = await self._uow.administration.create_school(actor_id, details)
            await self._uow.administration.install_admin(school_id, account.id)
            now = self._clock.now()
            await self._uow.activations.queue_initial(
                account.id, school_id, actor_id, now, now + self._limits.queue_ttl
            )
            result = account.id if operation == "school_admin" else school_id
            await self._uow.administration.finish_request(request_id, result)
            await self._uow.audit.record(
                school_id,
                actor_id,
                f"platform.{operation}",
                "schools",
                school_id,
                {"admin_id": str(account.id)},
            )
            return AdminInstallation(
                result, await self._uow.administration.pending_admin(school_id) is not None
            )
