from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.results import SessionReport
from nalar.application.ports.uow import UnitOfWork


class SessionReportQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, session_id: UUID) -> SessionReport:
        async with self._uow:
            if not await self._uow.authz.teaches_session(actor_id, session_id):
                raise NotFound()
            report = await self._uow.results.report(session_id)
        if report is None:
            raise NotFound()
        return report
