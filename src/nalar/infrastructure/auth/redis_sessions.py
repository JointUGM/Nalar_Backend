import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from nalar.application.errors import DependencyUnavailable, Unauthenticated
from nalar.application.ports.auth import AuthTokens, IdentityProvider


class RedisBrowserSessions:
    def __init__(
        self,
        redis: Redis,
        identity: IdentityProvider,
        lifetime_s: int,
        refresh_margin_s: int,
        refresh_lock_s: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._identity = identity
        self._lifetime_s = lifetime_s
        self._margin_s = refresh_margin_s
        self._lock_s = refresh_lock_s
        self._clock = clock

    async def create(self, tokens: AuthTokens) -> str:
        session_id = secrets.token_urlsafe(32)
        try:
            await self._redis.set(self._key(session_id), self._encode(tokens), ex=self._lifetime_s)
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc
        return session_id

    async def resolve(self, session_id: str) -> AuthTokens:
        key = self._key(session_id)
        lock_key = f"{key}:refresh"
        owner = secrets.token_urlsafe(16)
        deadline = time.monotonic() + self._lock_s
        try:
            while time.monotonic() < deadline:
                raw = await self._redis.get(key)
                if raw is None:
                    raise Unauthenticated()
                tokens = self._decode(raw)
                if tokens.expires_at > self._clock() + self._margin_s:
                    return tokens
                if not await self._redis.set(lock_key, owner, nx=True, ex=self._lock_s):
                    await asyncio.sleep(0.025)
                    continue
                try:
                    # Re-read after locking: another process may have refreshed or logged out.
                    raw = await self._redis.get(key)
                    if raw is None:
                        raise Unauthenticated()
                    tokens = self._decode(raw)
                    if tokens.expires_at > self._clock() + self._margin_s:
                        return tokens
                    try:
                        refreshed = await self._identity.refresh(tokens.refresh_token)
                    except Unauthenticated:
                        await self._redis.delete(key)
                        raise
                    if refreshed.user_id != tokens.user_id:
                        await self._redis.delete(key)
                        raise Unauthenticated()
                    async with self._redis.pipeline(transaction=True) as pipe:
                        await pipe.watch(key, lock_key)
                        if await pipe.get(key) != raw:
                            raise Unauthenticated()
                        if await pipe.get(lock_key) != owner.encode():
                            raise DependencyUnavailable()
                        pipe.multi()  # type: ignore[no-untyped-call]
                        pipe.set(key, self._encode(refreshed), keepttl=True)
                        await pipe.execute()
                    return refreshed
                finally:
                    await self._unlock(lock_key, owner)
            raise DependencyUnavailable()
        except WatchError as exc:
            raise Unauthenticated() from exc
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc

    async def delete(self, session_id: str) -> AuthTokens | None:
        try:
            raw = await self._redis.getdel(self._key(session_id))
            return self._decode(raw) if raw is not None else None
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc

    async def _unlock(self, key: str, owner: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            if await pipe.get(key) == owner.encode():
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.delete(key)
                with suppress(WatchError):
                    await pipe.execute()

    def _key(self, session_id: str) -> str:
        if len(session_id) > 128 or not session_id:
            raise Unauthenticated()
        return "session:" + hashlib.sha256(session_id.encode()).hexdigest()

    def _encode(self, tokens: AuthTokens) -> str:
        return json.dumps({**asdict(tokens), "user_id": str(tokens.user_id)})

    def _decode(self, raw: bytes | str) -> AuthTokens:
        try:
            value = json.loads(raw)
            return AuthTokens(
                UUID(value["user_id"]),
                value["access_token"],
                value["refresh_token"],
                value["expires_at"],
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise Unauthenticated() from exc
