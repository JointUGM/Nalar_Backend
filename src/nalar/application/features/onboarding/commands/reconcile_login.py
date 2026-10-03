from uuid import UUID

from nalar.application.errors import DependencyUnavailable
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


class ReconcileOnboardingHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, authenticated_user_id: UUID) -> None:
        try:
            async with self._uow:
                await self._uow.administration.lock_admin_handoffs(authenticated_user_id)
                await self._uow.activations.reconcile_login(
                    authenticated_user_id, self._clock.now()
                )
                await self._uow.administration.complete_admin_handoffs(authenticated_user_id)
        except Exception as exc:
            raise DependencyUnavailable() from exc
