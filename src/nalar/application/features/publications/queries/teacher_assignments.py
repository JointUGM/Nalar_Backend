from uuid import UUID

from nalar.application.ports.publications import Assignment
from nalar.application.ports.uow import UnitOfWork


class TeacherAssignmentsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID) -> list[Assignment]:
        # The SQL returns only the caller's own active assignments: that is the authz check.
        async with self._uow:
            return await self._uow.publications.teacher_assignments(actor_id)
