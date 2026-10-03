import asyncio
import hashlib
from contextlib import suppress
from uuid import UUID, uuid4

from nalar.application.errors import AppError, DependencyUnavailable, InvalidActivation
from nalar.application.features.auth.commands.change_password import PasswordMutationHandler
from nalar.application.features.onboarding.commands.activate_account import ActivationPolicy
from nalar.application.ports.auth import IdentityProvider, RecoveryProof
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


class ConfirmPasswordResetHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        provider: IdentityProvider,
        mutation: PasswordMutationHandler,
        policy: ActivationPolicy,
    ) -> None:
        self._uow, self._clock, self._provider, self._mutation, self._policy = (
            uow,
            clock,
            provider,
            mutation,
            policy,
        )

    async def execute(self, reset_id: UUID, token_hash: str, password: str) -> None:
        self._policy.validate_password(password)
        token, now = uuid4(), self._clock.now()
        async with self._uow:
            recipient = await self._uow.password_resets.claim_proof(
                reset_id,
                hashlib.sha256(token_hash.encode()).hexdigest(),
                token,
                now,
                now + self._policy.lease,
            )
        if recipient is None:
            raise InvalidActivation()
        proof: RecoveryProof | None = None
        try:
            async with asyncio.timeout(self._policy.timeout_s):
                proof = await self._provider.verify_recovery(token_hash)
                if (
                    proof.user_id != recipient.user_id
                    or proof.email.lower() != recipient.email.lower()
                ):
                    raise InvalidActivation()
                await self._mutation.execute(recipient, proof, password, token, reset_id)
        except Exception as exc:
            async with self._uow:
                await self._uow.password_resets.fail_proof(reset_id, token)
            if isinstance(exc, AppError):
                raise
            raise DependencyUnavailable() from exc
        finally:
            if proof:
                with suppress(AppError, TimeoutError):
                    async with asyncio.timeout(self._policy.logout_timeout_s):
                        await self._provider.sign_out(proof.access_token)
