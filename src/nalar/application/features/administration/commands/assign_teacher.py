from uuid import UUID

from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.uow import UnitOfWork


class AssignTeacherHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        actor_id: UUID,
        school_id: UUID,
        class_id: UUID,
        subject_id: UUID,
        teacher_id: UUID | None,
    ) -> None:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            await self._uow.administration.assign_teacher(
                school_id, class_id, subject_id, teacher_id
            )
            await self._uow.audit.record(
                school_id,
                actor_id,
                "admin.assign_teacher",
                "classes",
                class_id,
                {
                    "school_subject_id": str(subject_id),
                    "teacher_id": str(teacher_id) if teacher_id else None,
                },
            )
