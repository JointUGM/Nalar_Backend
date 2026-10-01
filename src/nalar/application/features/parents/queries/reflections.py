from dataclasses import dataclass
from uuid import UUID

from nalar.application.cursor import decode_cursor, encode_cursor
from nalar.application.errors import NotFound
from nalar.application.ports.parents import ParentReflection
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ReflectionPage:
    items: list[ParentReflection]
    next_cursor: str | None


class ParentReflectionsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, parent_id: UUID, student_id: UUID, limit: int, cursor: str | None
    ) -> ReflectionPage:
        after = decode_cursor(cursor) if cursor else None
        async with self._uow:
            if not await self._uow.authz.is_linked_parent(parent_id, student_id):
                raise NotFound()
            rows = await self._uow.parents.reflections(student_id, limit + 1, after)
        items = rows[:limit]
        last = items[-1] if len(rows) > limit else None
        next_cursor = encode_cursor(last.completed_at, last.session_id) if last else None
        return ReflectionPage(items, next_cursor)
