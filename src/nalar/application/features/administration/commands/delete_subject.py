from uuid import UUID

from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.uow import UnitOfWork


class DeleteSubjectHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, school_id: UUID, subject_id: UUID) -> None:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            name = await self._uow.administration.delete_subject(school_id, subject_id)
            await self._uow.audit.record(
                school_id,
                actor_id,
                "subject.deleted",
                "school_subjects",
                subject_id,
                {"name": name},
            )
