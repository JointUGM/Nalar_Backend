from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.features.evaluation.messages import evaluation_message
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import EVAL_QUEUE
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import SessionStatus


class EndSessionHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, actor_id: UUID, session_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.authz.teaches_session(actor_id, session_id):
                raise NotFound()
            if await self._uow.sessions.end_teacher(session_id, self._clock.now()):
                await self._uow.queue.send(EVAL_QUEUE, evaluation_message(session_id))
                await self._uow.audit.session_action(
                    session_id, actor_id, "session.teacher_ended", {}
                )
                return
            session = await self._uow.sessions.get_ref(session_id)
            if session is None:
                raise NotFound()
            if session.status is SessionStatus.paused_safety:
                raise Conflict("SESSION_PAUSED_USE_SAFETY_ACTION")
