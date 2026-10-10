from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.features.integrity.messages import live_session_flags_message
from nalar.application.ports.background import BackgroundWork
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class SubmitAnswer:
    actor_id: UUID
    session_id: UUID
    turn_index: int
    answer_text: str
    client_submission_id: UUID


@dataclass(frozen=True)
class Accepted:
    session_id: UUID


class SubmitAnswerHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock, background: BackgroundWork) -> None:
        self._uow = uow
        self._clock = clock
        self._background = background

    async def execute(self, cmd: SubmitAnswer) -> Accepted:
        async with self._uow:
            uow = self._uow
            if not await uow.authz.owns_session(cmd.actor_id, cmd.session_id):
                raise NotFound()
            if await self._replayed(cmd):
                return Accepted(cmd.session_id)
            now = self._clock.now()
            if not await uow.turns.save_answer(
                cmd.session_id, cmd.turn_index, cmd.answer_text, cmd.client_submission_id, now
            ):
                session = await uow.sessions.get_ref(cmd.session_id)
                if session is not None and now >= session.deadline_at:
                    raise Conflict("SESSION_DEADLINE_PASSED")
                if await self._replayed(cmd):
                    return Accepted(cmd.session_id)
                if await uow.turns.is_answered(cmd.session_id, cmd.turn_index):
                    raise Conflict("TURN_ALREADY_ANSWERED")
                raise Conflict("TURN_NOT_CURRENT")
            await uow.sessions.touch(cmd.session_id, now)
            await uow.queue.send(DEFAULT_QUEUE, live_session_flags_message(cmd.session_id))
        # A replay returns above without a second turn step; the state read re-kicks a lost one.

        self._background.run_turn_step(cmd.session_id, cmd.turn_index)
        return Accepted(cmd.session_id)

    async def _replayed(self, cmd: SubmitAnswer) -> bool:
        turn = await self._uow.turns.submission_turn(cmd.session_id, cmd.client_submission_id)
        if turn is None:
            return False
        if turn != cmd.turn_index:
            raise Conflict("TURN_ALREADY_ANSWERED")
        return True
