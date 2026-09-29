import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from nalar.infrastructure.queue.pgmq import PgmqConsumer, PgmqSender
from nalar.presentation.worker.runner import Handler, QueueLoop
from tests.integration.conftest import TEST_DATABASE_URL


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest.fixture
async def queue_name(pool: asyncpg.Pool) -> AsyncIterator[str]:
    name = f"test_{uuid4().hex[:12]}"
    async with pool.acquire() as conn:
        await conn.execute("select pgmq.create($1)", name)
    yield name
    async with pool.acquire() as conn:
        await conn.execute("select pgmq.drop_queue($1)", name)


async def send(pool: asyncpg.Pool, queue: str, body: dict[str, Any]) -> int:
    async with pool.acquire() as conn:
        return await PgmqSender(conn).send(queue, body)


async def remaining(pool: asyncpg.Pool, queue: str) -> tuple[int, int]:
    async with pool.acquire() as conn:
        live = await conn.fetchval(f"select count(*) from pgmq.q_{queue}")
        archived = await conn.fetchval(f"select count(*) from pgmq.a_{queue}")
    return live, archived


async def run_until(loop: QueueLoop, condition: Any, timeout_s: float = 10.0) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run(stop))
    try:
        async with asyncio.timeout(timeout_s):
            while not await condition():
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await task


async def test_message_is_handled_once_and_deleted(pool: asyncpg.Pool, queue_name: str) -> None:
    handled: list[dict[str, Any]] = []

    async def handle(body: dict[str, Any]) -> None:
        handled.append(body)

    await send(pool, queue_name, {"kind": "demo", "n": 1})
    loop = QueueLoop(queue_name, PgmqConsumer(pool), {"demo": handle}, 2, 30, idle_sleep_s=0.05)

    async def done() -> bool:
        return await remaining(pool, queue_name) == (0, 0)

    await run_until(loop, done)
    assert handled == [{"kind": "demo", "n": 1}]


async def test_failing_message_is_retried_then_archived(
    pool: asyncpg.Pool, queue_name: str
) -> None:
    attempts = 0

    async def always_fails(body: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    await send(pool, queue_name, {"kind": "demo"})
    loop = QueueLoop(
        queue_name,
        PgmqConsumer(pool),
        {"demo": always_fails},
        1,
        0,
        max_reads=3,
        idle_sleep_s=0.05,
    )

    async def archived() -> bool:
        return await remaining(pool, queue_name) == (0, 1)

    await run_until(loop, archived)
    assert attempts == 3


async def test_unknown_kind_is_archived_without_retry(pool: asyncpg.Pool, queue_name: str) -> None:
    await send(pool, queue_name, {"kind": "nobody_handles_this"})
    loop = QueueLoop(queue_name, PgmqConsumer(pool), {}, 1, 30, idle_sleep_s=0.05)

    async def archived() -> bool:
        return await remaining(pool, queue_name) == (0, 1)

    await run_until(loop, archived)


async def test_busy_slots_do_not_idle_the_loop(pool: asyncpg.Pool, queue_name: str) -> None:
    async def handle(body: dict[str, Any]) -> None:
        await asyncio.sleep(0.01)

    for n in range(20):
        await send(pool, queue_name, {"kind": "demo", "n": n})
    loop = QueueLoop(queue_name, PgmqConsumer(pool), {"demo": handle}, 2, 30, idle_sleep_s=1.0)

    async def drained() -> bool:
        return await remaining(pool, queue_name) == (0, 0)

    await run_until(loop, drained, timeout_s=3.0)


async def test_concurrency_limit_is_respected(pool: asyncpg.Pool, queue_name: str) -> None:
    in_flight = 0
    peak = 0
    handlers: dict[str, Handler] = {}

    async def slow(body: dict[str, Any]) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.2)
        in_flight -= 1

    handlers["demo"] = slow
    for n in range(6):
        await send(pool, queue_name, {"kind": "demo", "n": n})
    loop = QueueLoop(queue_name, PgmqConsumer(pool), handlers, 2, 30, idle_sleep_s=0.05)

    async def drained() -> bool:
        return await remaining(pool, queue_name) == (0, 0)

    await run_until(loop, drained)
    assert peak == 2
