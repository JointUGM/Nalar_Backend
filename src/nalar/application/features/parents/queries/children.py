from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import InvalidInput
from nalar.application.ports.parents import Child
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ChildrenPage:
    items: list[Child]
    next_cursor: str | None


class ChildrenQuery:
    """Linked children only; a user with no links gets an empty list."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, parent_id: UUID, limit: int, cursor: str | None) -> ChildrenPage:
        try:
            after = UUID(cursor) if cursor else None
        except ValueError as exc:
            raise InvalidInput(details={"cursor": "invalid"}) from exc
        async with self._uow:
            rows = await self._uow.parents.children(parent_id, limit + 1, after)
        items = rows[:limit]
        return ChildrenPage(items, str(items[-1].student_id) if len(rows) > limit else None)
