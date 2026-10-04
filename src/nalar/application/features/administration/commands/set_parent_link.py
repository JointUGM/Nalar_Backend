from uuid import UUID

from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.uow import UnitOfWork


class SetParentLinkHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        actor_id: UUID,
        school_id: UUID,
        parent_id: UUID,
        student_id: UUID,
        linked: bool,
        relationship: str | None = None,
    ) -> None:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            await self._uow.administration.set_parent_link(
                school_id, parent_id, student_id, linked, relationship
            )
            await self._uow.audit.record(
                school_id,
                actor_id,
                "admin.set_parent_link",
                "profiles",
                parent_id,
                {"student_id": str(student_id), "linked": linked},
            )
