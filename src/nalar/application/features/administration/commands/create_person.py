import asyncio
import hashlib
import json
from dataclasses import asdict
from uuid import UUID

from nalar.application.errors import Conflict, DependencyUnavailable
from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.features.onboarding.commands.request_invitations import (
    InvitationRequestLimits,
)
from nalar.application.features.roster.commands.upload_roster import RosterLimits
from nalar.application.ports.administration import NewPerson
from nalar.application.ports.auth_admin import AuthAdmin, AuthAdminError
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


class CreatePersonHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        admin: AuthAdmin,
        clock: Clock,
        limits: InvitationRequestLimits,
        roster_limits: RosterLimits,
    ) -> None:
        self._uow, self._admin, self._clock = uow, admin, clock
        self._limits, self._roster_limits = limits, roster_limits

    async def execute(self, actor_id: UUID, school_id: UUID, details: NewPerson, key: UUID) -> UUID:
        digest = hashlib.sha256(
            json.dumps(asdict(details), sort_keys=True, default=str).encode()
        ).hexdigest()
        email = details.email or f"{details.nisn}@{self._roster_limits.student_login_domain}"
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            _, previous = await self._uow.administration.request(
                actor_id, "create_person", school_id, key, digest
            )
            if previous:
                return previous
            await self._uow.administration.validate_person(school_id, details)
            existing = await self._uow.roster.profile_by_email(email)
            if details.nisn:
                student = await self._uow.roster.student_by_nisn(details.nisn)
                if student is not None and student != existing:
                    raise Conflict("PERSON_IDENTITY_CONFLICT")
        try:
            async with asyncio.timeout(self._limits.lookup_timeout_s):
                account = (
                    await self._admin.get_account(existing)
                    if existing
                    else await self._admin.create_or_find(email, details.full_name)
                )
        except (AuthAdminError, TimeoutError) as exc:
            raise DependencyUnavailable() from exc
        if account is None or account.email is None or account.email.lower() != email.lower():
            raise DependencyUnavailable()
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            request_id, previous = await self._uow.administration.request(
                actor_id, "create_person", school_id, key, digest
            )
            if previous:
                return previous
            await self._uow.administration.validate_person(school_id, details)
            await self._uow.roster.ensure_profile(
                account.id,
                details.full_name,
                details.email,
                details.email is not None,
                onboarding_required=details.email is not None and account.last_sign_in_at is None,
            )
            if details.role == "student":
                assert details.nisn is not None and details.class_id is not None
                if not await self._uow.roster.claim_nisn(account.id, details.nisn):
                    raise Conflict("PERSON_IDENTITY_CONFLICT")
            if details.role != "parent" and not await self._uow.roster.ensure_membership(
                school_id, account.id, details.role
            ):
                raise Conflict("PERSON_INACTIVE")
            if details.class_id:
                await self._uow.administration.place_students(
                    school_id, details.class_id, [account.id]
                )
            for student_id in details.child_ids:
                await self._uow.administration.attach_parent(
                    school_id, account.id, student_id, details.relationship, restore=False
                )
            if details.invite:
                now = self._clock.now()
                await self._uow.activations.queue_initial(
                    account.id, school_id, actor_id, now, now + self._limits.queue_ttl
                )
            await self._uow.administration.finish_request(request_id, account.id)
            await self._uow.audit.record(
                school_id,
                actor_id,
                "admin.create_person",
                "profiles",
                account.id,
                {"role": details.role},
            )
            return account.id
