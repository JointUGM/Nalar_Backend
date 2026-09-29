import asyncpg
from asyncpg.pool import PoolConnectionProxy
from pgvector.asyncpg import register_vector

type DbConnection = asyncpg.Connection | PoolConnectionProxy


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


async def create_pool(
    dsn: str,
    min_size: int,
    max_size: int,
    statement_cache_size: int,
    command_timeout: float,
) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        statement_cache_size=statement_cache_size,
        command_timeout=command_timeout,
        init=_init_connection,
    )
