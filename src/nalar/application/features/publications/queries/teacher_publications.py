from dataclasses import dataclass
from uuid import UUID

from nalar.application.cursor import decode_cursor, encode_cursor
from nalar.application.errors import NotFound
from nalar.application.ports.publications import PublicationSummary
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class TeacherPublications:
    actor_id: UUID
    class_id: UUID | None
    limit: int
    cursor: str | None
    search: str = ""
    status: str | None = None
    school_subject_id: UUID | None = None


@dataclass(frozen=True)
class PublicationPage:
    items: list[PublicationSummary]
    next_cursor: str | None


class TeacherPublicationsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, q: TeacherPublications) -> PublicationPage:
        after = decode_cursor(q.cursor) if q.cursor else None
        async with self._uow:
            if q.class_id is not None and not await self._uow.authz.teaches_class(
                q.actor_id, q.class_id
            ):
                raise NotFound()
            rows = await self._uow.publications.teacher_publications(
                q.actor_id, q.class_id, q.limit + 1, after, q.search, q.status, q.school_subject_id
            )
        items = rows[: q.limit]
        return PublicationPage(
            items,
            encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > q.limit else None,
        )
