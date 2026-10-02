import asyncio
from uuid import UUID, uuid4

import asyncpg

from nalar.application.ports.notifications import PendingDigest
from nalar.infrastructure.db.repositories.notifications import PgNotificationsRepo
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.factories import build_world
from tests.integration.test_digest import TIMING
from tests.unit.application.fakes import FakeClock


async def test_competing_workers_share_one_sender_lease(pool: asyncpg.Pool) -> None:
    now, sender_key = FakeClock().now(), str(uuid4())
    async with pool.acquire() as conn, conn.transaction():
        world = await build_world(conn)
        repo = PgNotificationsRepo(conn)
        ids: list[UUID] = []
        for _ in range(2):
            key = f"competing:{uuid4()}"
            await repo.queue_digest(
                world.parent_id, world.school_id, key, [], now, now + TIMING.retry_window
            )
            ids.append(
                await conn.fetchval("select id from notifications where dedupe_key = $1", key)
            )

    async def claim(digest_id: UUID) -> PendingDigest | None:
        async with PgUnitOfWork(pool.acquire) as uow:
            return await uow.notifications.claim_digest(
                digest_id,
                now,
                now + TIMING.lease,
                uuid4(),
                {},
                sender_key=sender_key,
                daily_limit=TIMING.daily_limit,
                sender_spacing=TIMING.sender_spacing,
            )

    try:
        results = await asyncio.gather(*(claim(digest_id) for digest_id in ids))
        assert sum(result is not None for result in results) == 1
        async with pool.acquire() as conn:
            attempts = await conn.fetchval(
                "select sum(coalesce((payload->>'attempts')::int, 0))"
                " from notifications where id = any($1::uuid[])",
                ids,
            )
            assert attempts == 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute("delete from notifications where id = any($1::uuid[])", ids)
