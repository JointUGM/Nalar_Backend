import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

T = TypeVar("T")

_CLOCK_SKEW_S = 60


class RedisAuthState:
    """Session revocations and login attempts in Redis; both fail open while it is unreachable."""

    def __init__(
        self,
        redis: Redis,
        access_token_ttl_s: int,
        login_per_minute: int,
        clock: Callable[[], float] = time.time,
        backoff_s: float = 5.0,
    ) -> None:
        self._redis = redis
        self._revoke_for_s = access_token_ttl_s + _CLOCK_SKEW_S
        self._login_per_minute = login_per_minute
        self._clock = clock
        self._backoff_s = backoff_s
        self._skip_until = 0.0

    async def revoke(self, session_id: UUID) -> None:
        # A token refreshed in another tab just before logout outlives the one presented (D-AUTH-5).
        await self._run(
            lambda: self._redis.set(f"revoked:{session_id}", 1, ex=self._revoke_for_s), None
        )

    async def is_revoked(self, session_id: UUID) -> bool:
        return bool(await self._run(lambda: self._redis.exists(f"revoked:{session_id}"), 0))

    async def allow(self, email: str) -> bool:
        digest = hashlib.sha256(email.encode()).hexdigest()[:32]
        key = f"login:{digest}:{int(self._clock() // 60)}"

        async def count() -> int:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, 120)
                hits, _ = await pipe.execute()
            return int(hits)

        return await self._run(count, 0) <= self._login_per_minute

    async def _run(self, command: Callable[[], Awaitable[T]], fallback: T) -> T:
        # D-AUTH-6: a live class must not stop for Redis; skip it briefly instead of timing out.
        if self._clock() < self._skip_until:
            return fallback
        try:
            return await command()
        except (RedisError, OSError) as exc:
            self._skip_until = self._clock() + self._backoff_s
            log.warning(
                "redis unavailable, auth checks fail open for %ss: %s",
                self._backoff_s,
                type(exc).__name__,
            )
            return fallback
