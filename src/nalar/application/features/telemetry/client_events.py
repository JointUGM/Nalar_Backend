from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from nalar.application.errors import InvalidInput, NotFound
from nalar.application.ports.client_events import ClientEvent
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ClientEventPolicy:
    client_events_per_minute: int
    client_vitals_sample_rate: float
    max_age_s: int
    future_skew_s: int


class ClientEventsHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock, policy: ClientEventPolicy) -> None:
        self._uow, self._clock, self._policy = uow, clock, policy

    async def execute(self, actor_id: UUID, events: list[ClientEvent]) -> int:
        async with self._uow:
            me = await self._uow.identity.me(actor_id)
            if me is None or not (me.roles or me.is_parent or me.is_platform_admin):
                raise NotFound()
            now = self._clock.now()
            if any(
                not now - timedelta(seconds=self._policy.max_age_s)
                <= event.occurred_at
                <= now + timedelta(seconds=self._policy.future_skew_s)
                for event in events
            ):
                raise InvalidInput("EVENT_TIME_INVALID")
            sampled = [
                event
                for event in events
                if event.kind == "error"
                or event.event_id.int / (1 << 128) < self._policy.client_vitals_sample_rate
            ]
            return await self._uow.client_events.insert(
                actor_id, sampled, now, self._policy.client_events_per_minute
            )
