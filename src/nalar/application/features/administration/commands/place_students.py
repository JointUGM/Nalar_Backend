from uuid import UUID

from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.uow import UnitOfWork


class PlaceStudentsHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, school_id: UUID, class_id: UUID, user_ids: list[UUID]
    ) -> None:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            await self._uow.administration.place_students(school_id, class_id, user_ids)
            await self._uow.audit.record(
                school_id,
                actor_id,
                "admin.place_students",
                "classes",
                class_id,
                {"user_ids": [str(user_id) for user_id in user_ids]},
            )
