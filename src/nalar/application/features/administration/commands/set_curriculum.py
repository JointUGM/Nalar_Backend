from uuid import UUID

from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.uow import UnitOfWork


class SetCurriculumHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        actor_id: UUID,
        school_id: UUID,
        subject_id: UUID,
        version_id: UUID,
        cp_subject_id: UUID | None,
    ) -> None:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            await self._uow.administration.set_curriculum(
                school_id, subject_id, version_id, cp_subject_id
            )
            await self._uow.audit.record(
                school_id,
                actor_id,
                "admin.set_curriculum",
                "school_subjects",
                subject_id,
                {"cp_version_id": str(version_id)},
            )
