from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.parents import VisibleResult
from nalar.application.ports.uow import UnitOfWork

_UNDERSTOOD = frozenset({"mastered"})


@dataclass(frozen=True)
class ChildProgress:
    sessions_completed: int
    concepts_understood: list[str]
    concepts_developing: list[str]
    summaries: list[VisibleResult]


class ProgressQuery:
    """PA-1/PA-2. A misconception reads as "developing": parents never see diagnostic labels."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, parent_id: UUID, student_id: UUID) -> ChildProgress:
        async with self._uow:
            if not await self._uow.authz.is_linked_parent(parent_id, student_id):
                raise NotFound()
            results = await self._uow.parents.visible_results(student_id)
            concepts = await self._uow.parents.concepts_seen(student_id)
        return ChildProgress(
            sessions_completed=len(results),
            concepts_understood=[n for n, o in concepts if o in _UNDERSTOOD],
            concepts_developing=[n for n, o in concepts if o not in _UNDERSTOOD],
            summaries=results,
        )
