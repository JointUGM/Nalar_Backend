from dataclasses import dataclass
from uuid import UUID

from nalar.application.cursor import decode_cursor, encode_cursor
from nalar.application.errors import NotFound
from nalar.application.ports.missions import MissionSummary
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ListMissions:
    actor_id: UUID
    school_id: UUID
    school_subject_id: UUID | None
    limit: int
    cursor: str | None
    search: str = ""
    status: str | None = None


@dataclass(frozen=True)
class MissionPage:
    items: list[MissionSummary]
    next_cursor: str | None


class ListMissionsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, q: ListMissions) -> MissionPage:
        after = decode_cursor(q.cursor) if q.cursor else None
        async with self._uow:
            if not await self._uow.authz.is_school_teacher(q.actor_id, q.school_id):
                raise NotFound()
            rows = await self._uow.missions.mission_page(
                q.actor_id, q.school_id, q.school_subject_id, q.limit + 1, after, q.search, q.status
            )
        items = rows[: q.limit]
        last = items[-1] if len(rows) > q.limit else None
        return MissionPage(items, encode_cursor(last.created_at, last.id) if last else None)
