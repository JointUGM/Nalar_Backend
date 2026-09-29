from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.identity import Me
from nalar.application.ports.uow import UnitOfWork


class MeQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, user_id: UUID) -> Me:
        async with self._uow:
            me = await self._uow.identity.me(user_id)
        if me is None:
            raise NotFound()
        return me
