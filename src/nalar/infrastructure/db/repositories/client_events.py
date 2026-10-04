from datetime import datetime, timedelta
from uuid import UUID

from nalar.application.errors import TooManyRequests
from nalar.application.ports.client_events import ClientEvent
from nalar.infrastructure.db.pool import DbConnection


class PgClientEventsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def insert(
        self, actor_id: UUID, events: list[ClientEvent], now: datetime, per_minute: int
    ) -> int:
        await self._conn.execute(
            "select pg_advisory_xact_lock(hashtextextended($1, 0))", f"client-events:{actor_id}"
        )
        count = await self._conn.fetchval(
            "select count(*) from client_events where actor_id = $1 and received_at >= $2",
            actor_id,
            now - timedelta(minutes=1),
        )
        if count + len(events) > per_minute:
            raise TooManyRequests()
        accepted = 0
        for event in events:
            accepted += int(
                await self._conn.fetchval(
                    "insert into "
                    "client_events(actor_id,event_id,kind,code,route,value,occurred_at,received_at)"
                    " values($1,$2,$3,$4,$5,$6,$7,$8) on conflict do nothing returning event_id",
                    actor_id,
                    event.event_id,
                    event.kind,
                    event.code,
                    event.route,
                    event.value,
                    event.occurred_at,
                    now,
                )
                is not None
            )
        return accepted
