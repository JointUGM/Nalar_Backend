import asyncio
import hashlib
import logging
import string
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from nalar.application.errors import (
    AppError,
    DependencyUnavailable,
    InvalidActivation,
    InvalidInput,
)
from nalar.application.ports.auth import IdentityProvider, RecoveryProof
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivationPolicy:
    minimum_password_length: int
    required_characters: tuple[str, ...]
    timeout_s: float
    lease: timedelta
    logout_timeout_s: float

    def validate_password(self, password: str) -> None:
        groups = {
            "lowercase": string.ascii_lowercase,
            "uppercase": string.ascii_uppercase,
            "digit": string.digits,
            "symbol": string.punctuation,
        }
        if len(password) < self.minimum_password_length or any(
            not any(char in groups[group] for char in password)
            for group in self.required_characters
        ):
            raise InvalidInput(
                "WEAK_PASSWORD",
                "Kata sandi tidak memenuhi kebijakan keamanan.",
                details={
                    "minimum_length": self.minimum_password_length,
                    "required_characters": list(self.required_characters),
                },
            )


class ActivateAccountHandler:
    def __init__(
        self, uow: UnitOfWork, clock: Clock, provider: IdentityProvider, policy: ActivationPolicy
    ) -> None:
        self._uow, self._clock, self._provider, self._policy = uow, clock, provider, policy

    async def execute(self, activation_id: UUID, token_hash: str, password: str) -> None:
        self._policy.validate_password(password)
        token, now = uuid4(), self._clock.now()
        digest = hashlib.sha256(token_hash.encode()).hexdigest()
        async with self._uow:
            invitation = await self._uow.activations.claim_activation(
                activation_id,
                digest,
                token,
                now,
                now + self._policy.lease,
            )
        if invitation is None:
            raise InvalidActivation()
        proof: RecoveryProof | None = None
        verified = False
        try:
            async with asyncio.timeout(self._policy.timeout_s):
                proof = await self._provider.verify_recovery(token_hash)
                verified = True
                if (
                    proof.user_id != invitation.user_id
                    or proof.email.lower() != invitation.email.lower()
                ):
                    raise InvalidActivation()
                async with self._uow:
                    if not await self._uow.activations.checkpoint_activation(
                        invitation, token, self._clock.now()
                    ):
                        raise InvalidActivation()
                await self._provider.update_password(proof, password)
                async with self._uow:
                    if not await self._uow.activations.complete_activation(
                        invitation, token, self._clock.now()
                    ):
                        raise DependencyUnavailable()
                    await self._uow.audit.record(
                        invitation.school_id,
                        proof.user_id,
                        "account_activation.complete",
                        "account_activations",
                        activation_id,
                        {},
                    )
        except Exception as exc:
            try:
                async with self._uow:
                    await self._uow.activations.abort_activation(
                        invitation.id, token, failed=verified or isinstance(exc, InvalidActivation)
                    )
            except Exception:
                log.warning("activation outcome recording unavailable")
            if isinstance(exc, AppError):
                raise
            raise DependencyUnavailable() from exc
        finally:
            if proof:
                try:
                    async with asyncio.timeout(self._policy.logout_timeout_s):
                        await self._provider.sign_out(proof.access_token)
                except (AppError, TimeoutError):
                    log.warning("temporary recovery session logout unavailable")
