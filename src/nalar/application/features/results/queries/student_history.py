from uuid import UUID

from nalar.application.cursor import decode_cursor, encode_cursor
from nalar.application.errors import NotFound
from nalar.application.ports.administration import AdminRow
from nalar.application.ports.uow import UnitOfWork


class StudentHistoryQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, student_id: UUID, limit: int, cursor: str | None
    ) -> AdminRow:
        async with self._uow:
            if not await self._uow.authz.teaches_student(actor_id, student_id):
                raise NotFound()
            rows = await self._uow.results.student_history(
                actor_id, student_id, limit + 1, decode_cursor(cursor) if cursor else None
            )
        return {
            "items": rows[:limit],
            "next_cursor": encode_cursor(
                rows[limit - 1]["started_at"], rows[limit - 1]["session_id"]
            )
            if len(rows) > limit
            else None,
        }
