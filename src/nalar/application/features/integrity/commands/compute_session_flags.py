from uuid import UUID

from nalar.application.ports.uow import UnitOfWork
from nalar.domain.integrity import IntegrityConfig, TurnFacts, session_flags
from nalar.domain.telemetry import TurnMetrics, metrics_by_turn


class ComputeSessionFlagsHandler:
    """E1 per session (B17): telemetry → turn_metrics → flags. Safe to run twice (NFR-R3)."""

    def __init__(self, uow: UnitOfWork, config: IntegrityConfig) -> None:
        self._uow = uow
        self._config = config

    async def execute(self, session_id: UUID) -> int:
        async with self._uow:
            if not await self._uow.integrity.lock_session(session_id):
                return 0
            data = await self._uow.integrity.session_input(session_id)
            if data is None:
                return 0
            metrics = metrics_by_turn(data.batches)
            await self._uow.integrity.upsert_metrics(data.school_id, metrics)
            turns = [
                TurnFacts(
                    turn_id,
                    index,
                    answer,
                    data.quality.get(turn_id),
                    paused,
                    metrics.get(turn_id, TurnMetrics()),
                )
                for turn_id, index, answer, paused in data.turns
            ]
            return await self._uow.integrity.insert_flags(
                data.school_id, session_id, session_flags(turns, self._config)
            )
