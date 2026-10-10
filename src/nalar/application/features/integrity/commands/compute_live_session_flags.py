from uuid import UUID

from nalar.application.features.integrity.messages import session_flags_message
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.integrity import IntegrityConfig, TurnFacts, activity_flags
from nalar.domain.labels import SessionStatus
from nalar.domain.sessions import TERMINAL_STATUSES
from nalar.domain.telemetry import TurnMetrics, metrics_by_turn


class ComputeLiveSessionFlagsHandler:
    """Live E1 telemetry rules for active sessions; serializes through session lock."""

    def __init__(self, uow: UnitOfWork, config: IntegrityConfig) -> None:
        self._uow = uow
        self._config = config

    async def execute(self, session_id: UUID) -> int:
        async with self._uow:
            if not await self._uow.integrity.lock_session(session_id):
                return 0
            data = await self._uow.integrity.activity_input(session_id)
            if data is None:
                return 0
            if data.status in TERMINAL_STATUSES:
                if data.evaluated:
                    await self._uow.queue.send(DEFAULT_QUEUE, session_flags_message(session_id))
                return 0
            if data.status is not SessionStatus.in_progress:
                return 0
            metrics = metrics_by_turn(data.batches)
            await self._uow.integrity.upsert_metrics(data.school_id, metrics)
            turns = [
                TurnFacts(
                    turn_id,
                    index,
                    answer,
                    None,
                    paused,
                    metrics.get(turn_id, TurnMetrics()),
                )
                for turn_id, index, answer, paused in data.turns
            ]
            return await self._uow.integrity.insert_flags(
                data.school_id, session_id, activity_flags(turns, self._config)
            )
