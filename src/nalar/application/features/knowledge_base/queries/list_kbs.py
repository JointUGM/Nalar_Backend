from dataclasses import dataclass
from uuid import UUID

from nalar.application.cursor import decode_cursor, encode_cursor
from nalar.application.errors import NotFound
from nalar.application.ports.knowledge import KbSummary
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ListKbs:
    actor_id: UUID
    school_id: UUID
    school_subject_id: UUID | None
    limit: int
    cursor: str | None


@dataclass(frozen=True)
class KbPage:
    items: list[KbSummary]
    next_cursor: str | None


class ListKbsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, q: ListKbs) -> KbPage:
        after = decode_cursor(q.cursor) if q.cursor else None
        async with self._uow:
            if not await self._uow.authz.is_school_teacher(q.actor_id, q.school_id):
                raise NotFound()
            rows = await self._uow.knowledge.kb_page(
                q.actor_id, q.school_id, q.school_subject_id, q.limit + 1, after
            )
        items = rows[: q.limit]
        last = items[-1] if len(rows) > q.limit else None
        return KbPage(items, encode_cursor(last.created_at, last.id) if last else None)
