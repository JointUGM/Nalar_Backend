from datetime import datetime
from uuid import UUID

from nalar.infrastructure.db.pool import DbConnection

_INSERT = """
    insert into telemetry_batches
           (school_id, session_id, turn_id, client_seq, client_sent_at, events)
    select s.school_id, s.id,
           (select t.id from session_turns t where t.session_id = s.id and t.turn_index = $3),
           $2, $5, $4::jsonb
      from sessions s where s.id = $1
    on conflict (session_id, client_seq) do nothing
"""


class PgTelemetryRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def insert(
        self,
        session_id: UUID,
        client_seq: int,
        turn_index: int | None,
        events_json: str,
        client_sent_at: datetime | None,
    ) -> None:
        await self._conn.execute(
            _INSERT, session_id, client_seq, turn_index, events_json, client_sent_at
        )
