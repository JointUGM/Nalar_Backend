from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.results import ClassStudent
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ClassStudents:
    actor_id: UUID
    class_id: UUID
    publication_id: UUID | None = None


class ClassStudentsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, q: ClassStudents) -> list[ClassStudent]:
        async with self._uow:
            if not await self._uow.authz.teaches_class(q.actor_id, q.class_id):
                raise NotFound()
            if q.publication_id is not None and not await self._uow.authz.teaches_publication(
                q.actor_id, q.publication_id
            ):
                raise NotFound()
            rows = await self._uow.results.class_students(q.class_id, q.publication_id)
        if rows is None:
            raise NotFound()
        return rows
