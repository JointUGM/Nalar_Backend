from uuid import UUID

from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.uow import UnitOfWork


class EditPersonHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        actor_id: UUID,
        school_id: UUID,
        user_id: UUID,
        full_name: str | None,
        class_id: UUID | None,
    ) -> None:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            await self._uow.administration.edit_person(school_id, user_id, full_name, class_id)
            await self._uow.audit.record(
                school_id,
                actor_id,
                "admin.edit_person",
                "profiles",
                user_id,
                {
                    "fields": [
                        k
                        for k, v in {"full_name": full_name, "class_id": class_id}.items()
                        if v is not None
                    ]
                },
            )
