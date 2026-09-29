import base64
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import InvalidInput, NotFound
from nalar.application.ports.publications import PublicationSummary
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class TeacherPublications:
    actor_id: UUID
    class_id: UUID | None
    limit: int
    cursor: str | None


@dataclass(frozen=True)
class PublicationPage:
    items: list[PublicationSummary]
    next_cursor: str | None


class TeacherPublicationsQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, q: TeacherPublications) -> PublicationPage:
        after = _decode(q.cursor) if q.cursor else None
        async with self._uow:
            if q.class_id is not None and not await self._uow.authz.teaches_class(
                q.actor_id, q.class_id
            ):
                raise NotFound()
            rows = await self._uow.publications.teacher_publications(
                q.actor_id, q.class_id, q.limit + 1, after
            )
        items = rows[: q.limit]
        return PublicationPage(items, _encode(items[-1]) if len(rows) > q.limit else None)


def _encode(item: PublicationSummary) -> str:
    return base64.urlsafe_b64encode(f"{item.created_at.isoformat()}|{item.id}".encode()).decode()


def _decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        created_at, publication_id = base64.urlsafe_b64decode(cursor).decode().split("|")
        return datetime.fromisoformat(created_at), UUID(publication_id)
    except ValueError as exc:
        raise InvalidInput(details={"cursor": "invalid"}) from exc
