from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.ports.administration import AdminRow, ClassDetails
from nalar.application.ports.uow import UnitOfWork


class SaveClassHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, school_id: UUID, fields: AdminRow, class_id: UUID | None = None
    ) -> AdminRow:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            if class_id:
                previous = next(
                    (
                        c
                        for c in await self._uow.administration.classes(school_id, None)
                        if c["class_id"] == class_id
                    ),
                    None,
                )
                if previous is None:
                    raise NotFound()
                fields = {
                    k: previous[k]
                    for k in ("name", "grade_level", "academic_year_id", "homeroom_teacher_id")
                } | fields
            details = ClassDetails(**fields)
            result = await self._uow.administration.save_class(school_id, details, class_id)
            await self._uow.audit.record(
                school_id, actor_id, "admin.save_class", "classes", result, {"fields": list(fields)}
            )
            return next(
                c
                for c in await self._uow.administration.classes(school_id, details.academic_year_id)
                if c["class_id"] == result
            )
