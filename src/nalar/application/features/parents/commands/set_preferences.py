from uuid import UUID

from nalar.application.ports.uow import UnitOfWork


class SetPreferencesHandler:
    """Touches only the caller's own profile, so there is no link to check."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, user_id: UUID, enabled: bool) -> bool:
        async with self._uow:
            await self._uow.parents.set_digest(user_id, enabled)
        return enabled
