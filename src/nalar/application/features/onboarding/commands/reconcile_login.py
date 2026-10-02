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
                await self._uow.activations.reconcile_login(
                    authenticated_user_id, self._clock.now()
                )
        except Exception as exc:
            raise DependencyUnavailable() from exc
