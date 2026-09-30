import asyncio
import logging
from collections.abc import Awaitable, Callable

import asyncpg
import httpx

from nalar.application.ports.readiness import Readiness

log = logging.getLogger(__name__)


class PoolAndAiProbe:
    def __init__(self, pool: Callable[[], Awaitable[asyncpg.Pool]], ai_base_url: str) -> None:
        self._pool = pool
        self._ai_base_url = ai_base_url

    async def check(self) -> Readiness:
        database, ai = await asyncio.gather(self._database(), self._ai())
        return Readiness(database=database, ai=ai)

    async def _database(self) -> bool:
        # The pool is created lazily here, so a bad URL or an unreachable host reads as "down".
        try:
            async with asyncio.timeout(5):
                pool = await self._pool()
                async with pool.acquire() as conn:
                    return bool(await conn.fetchval("select 1"))
        except Exception:
            log.warning("readiness: database unavailable", exc_info=True)
            return False

    async def _ai(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self._ai_base_url, timeout=1.0) as http:
                return (await http.get("/health")).is_success
        except httpx.HTTPError:
            log.warning("readiness: AI service unavailable", exc_info=True)
            return False
