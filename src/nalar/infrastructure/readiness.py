import asyncio

import asyncpg
import httpx

from nalar.application.ports.readiness import Readiness


class PoolAndAiProbe:
    def __init__(self, pool: asyncpg.Pool, ai_base_url: str) -> None:
        self._pool = pool
        self._ai_base_url = ai_base_url

    async def check(self) -> Readiness:
        database, ai = await asyncio.gather(self._database(), self._ai())
        return Readiness(database=database, ai=ai)

    async def _database(self) -> bool:
        try:
            async with asyncio.timeout(2):
                async with self._pool.acquire() as conn:
                    return bool(await conn.fetchval("select 1"))
        except (OSError, asyncpg.PostgresError, TimeoutError):
            return False

    async def _ai(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self._ai_base_url, timeout=1.0) as http:
                return (await http.get("/health")).is_success
        except httpx.HTTPError:
            return False
