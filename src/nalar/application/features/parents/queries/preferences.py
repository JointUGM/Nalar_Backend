from uuid import UUID

from nalar.application.ports.uow import UnitOfWork


class PreferencesQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, user_id: UUID) -> bool:
        async with self._uow:
            return await self._uow.parents.digest_enabled(user_id)
