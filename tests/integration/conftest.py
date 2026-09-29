import os
from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests.integration.support.factories import World, build_world

TEST_DATABASE_URL = os.environ.get(
    "NALAR_TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
)


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(TEST_DATABASE_URL)
    transaction = connection.transaction()
    await transaction.start()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.fixture
async def world(conn: asyncpg.Connection) -> World:
    return await build_world(conn)


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=6)
    yield pool
    await pool.close()
