import asyncio
import logging
from contextlib import suppress
from uuid import UUID, uuid4

from nalar.application.errors import (
    AppError,
    DependencyUnavailable,
    InvalidActivation,
    InvalidCredentials,
    InvalidInput,
    NotFound,
)
from nalar.application.features.onboarding.commands.activate_account import ActivationPolicy
from nalar.application.ports.auth import (
    AuthTokens,
    BrowserSessions,
    IdentityProvider,
    RecoveryProof,
)
from nalar.application.ports.clock import Clock
from nalar.application.ports.password_resets import PasswordRecipient
from nalar.application.ports.uow import UnitOfWork

log = logging.getLogger(__name__)


class PasswordMutationHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        provider: IdentityProvider,
        sessions: BrowserSessions,
        policy: ActivationPolicy,
    ) -> None:
        self._uow, self._clock, self._provider, self._sessions, self._policy = (
            uow,
            clock,
            provider,
            sessions,
            policy,
        )

    async def execute(
        self,
        recipient: PasswordRecipient,
        proof: RecoveryProof,
        password: str,
        token: UUID,
        reset_id: UUID | None = None,
    ) -> None:
        now = self._clock.now()
        async with self._uow:
            if not await self._uow.password_resets.begin_mutation(
                recipient.user_id, token, now, now + self._policy.lease, reset_id
            ):
                raise DependencyUnavailable()
        submitting = False
        try:
            async with asyncio.timeout(self._policy.timeout_s):
                await self._sessions.revoke_user(recipient.user_id)
                async with self._uow:
                    if not await self._uow.password_resets.mutation_phase(
                        token, "password_submitting", self._clock.now()
                    ):
                        raise DependencyUnavailable()
                submitting = True
                await self._provider.update_password(proof, password)
                async with self._uow:
                    if not await self._uow.password_resets.mutation_phase(
                        token, "password_changed", self._clock.now()
                    ):
                        raise DependencyUnavailable()
                await self._provider.sign_out_all(proof.access_token)
                await self._sessions.revoke_user(recipient.user_id)
                async with self._uow:
                    if not await self._uow.password_resets.mutation_phase(
                        token, "completed", self._clock.now()
                    ):
                        raise DependencyUnavailable()
                    await self._uow.audit.record(
                        None, recipient.user_id, "password.change", "password_mutations", token, {}
                    )
        except Exception as exc:
            try:
                async with self._uow:
                    phase = (
                        "failed"
                        if not submitting or isinstance(exc, (InvalidInput, InvalidActivation))
                        else "unknown"
                    )
                    await self._uow.password_resets.mutation_phase(token, phase, self._clock.now())
            except Exception:
                log.warning("password mutation outcome recording unavailable")
            if isinstance(exc, AppError):
                raise
            raise DependencyUnavailable() from exc

    async def reconcile_login(self, tokens: AuthTokens) -> bool:
        async with self._uow:
            candidate = await self._uow.password_resets.reconcile_candidate(
                tokens.user_id, self._clock.now()
            )
        if candidate is None:
            return False
        async with asyncio.timeout(self._policy.timeout_s):
            await self._sessions.revoke_user(tokens.user_id)
            await self._provider.sign_out_all(tokens.access_token)
            async with self._uow:
                if not await self._uow.password_resets.mutation_phase(
                    candidate, "completed", self._clock.now()
                ):
                    raise DependencyUnavailable()
                await self._uow.audit.record(
                    None, tokens.user_id, "password.reconcile", "password_mutations", candidate, {}
                )
        return True


class ChangePasswordHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        provider: IdentityProvider,
        mutation: PasswordMutationHandler,
        policy: ActivationPolicy,
    ) -> None:
        self._uow, self._provider, self._mutation, self._policy = uow, provider, mutation, policy

    async def execute(self, actor_id: UUID, current_password: str, new_password: str) -> None:
        async with self._uow:
            recipient = await self._uow.password_resets.recipient(actor_id, recovery=False)
        if recipient is None:
            raise NotFound()
        self._policy.validate_password(new_password)
        tokens: AuthTokens | None = None
        try:
            async with asyncio.timeout(self._policy.timeout_s):
                tokens = await self._provider.sign_in(recipient.email, current_password)
                if tokens.user_id != actor_id:
                    raise InvalidCredentials()
                await self._mutation.execute(
                    recipient,
                    RecoveryProof(actor_id, recipient.email, tokens.access_token),
                    new_password,
                    uuid4(),
                )
        except TimeoutError as exc:
            raise DependencyUnavailable() from exc
        finally:
            if tokens:
                with suppress(AppError, TimeoutError):
                    async with asyncio.timeout(self._policy.logout_timeout_s):
                        await self._provider.sign_out(tokens.access_token)
