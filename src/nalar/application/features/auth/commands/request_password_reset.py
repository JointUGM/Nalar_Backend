from dataclasses import dataclass
from datetime import timedelta

from nalar.application.ports.auth import LoginAttempts
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class PasswordResetPolicy:
    enabled: bool
    redirect_url: str | None
    queue_lifetime: timedelta
    cooldown: timedelta


class RequestPasswordResetHandler:
    def __init__(
        self, uow: UnitOfWork, clock: Clock, attempts: LoginAttempts, policy: PasswordResetPolicy
    ) -> None:
        self._uow, self._clock, self._attempts, self._policy = uow, clock, attempts, policy

    async def execute(self, email: str, client_ip: str) -> None:
        email = email.strip().lower()
        address_allowed = await self._attempts.allow(f"password-reset:email:{email}")
        ip_allowed = await self._attempts.allow(f"password-reset:ip:{client_ip}")
        if not address_allowed or not ip_allowed or not self._policy.enabled:
            return
        now = self._clock.now()
        async with self._uow:
            reset_id = await self._uow.password_resets.request(
                email, now, now + self._policy.queue_lifetime, self._policy.cooldown
            )
            if reset_id:
                await self._uow.queue.send(
                    DEFAULT_QUEUE, {"kind": "send_password_reset", "reset_id": str(reset_id)}
                )
