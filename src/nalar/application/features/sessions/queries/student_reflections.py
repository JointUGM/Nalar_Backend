from dataclasses import dataclass
from uuid import UUID

from nalar.application.cursor import decode_cursor, encode_cursor
from nalar.application.ports.sessions import StudentReflection
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class StudentReflectionPage:
    items: list[StudentReflection]
    next_cursor: str | None


class StudentReflectionsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, limit: int, cursor: str | None
    ) -> StudentReflectionPage:
        after = decode_cursor(cursor) if cursor else None
        async with self._uow:
            rows = await self._uow.sessions.student_reflections(actor_id, limit + 1, after)
        items = rows[:limit]
        last = items[-1] if len(rows) > limit else None
        return StudentReflectionPage(
            items, encode_cursor(last.completed_at, last.session_id) if last else None
        )
