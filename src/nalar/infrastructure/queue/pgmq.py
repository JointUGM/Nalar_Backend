import json
from typing import Any

import asyncpg

from nalar.application.ports.queue import QueueMessage
from nalar.infrastructure.db.pool import DbConnection


class PgmqSender:
    """Sends on the caller's connection, so a message commits or rolls back with the transaction."""

    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def send(self, queue: str, body: dict[str, Any], delay_s: int = 0) -> int:
        # pgmq.send is overloaded on its third argument; the cast picks the integer delay.
        msg_id: int = await self._conn.fetchval(
            "select pgmq.send($1::text, $2::jsonb, $3::integer)", queue, json.dumps(body), delay_s
        )
        return msg_id


class PgmqConsumer:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def read(self, queue: str, visibility_timeout_s: int, limit: int) -> list[QueueMessage]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "select msg_id, read_ct, message"
                " from pgmq.read($1::text, $2::integer, $3::integer)",
                queue,
                visibility_timeout_s,
                limit,
            )
        return [
            QueueMessage(
                msg_id=row["msg_id"], read_count=row["read_ct"], body=json.loads(row["message"])
            )
            for row in rows
        ]

    async def delete(self, queue: str, msg_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("select pgmq.delete($1::text, $2::bigint)", queue, msg_id)

    async def archive(self, queue: str, msg_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("select pgmq.archive($1::text, $2::bigint)", queue, msg_id)
