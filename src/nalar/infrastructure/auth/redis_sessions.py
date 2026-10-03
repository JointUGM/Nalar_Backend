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
from nalar.application.ports.password_resets import CredentialState


class RedisBrowserSessions:
    def __init__(
        self,
        redis: Redis,
        identity: IdentityProvider,
        lifetime_s: int,
        refresh_margin_s: int,
        refresh_lock_s: int,
        clock: Callable[[], float] = time.time,
        credentials: CredentialState | None = None,
    ) -> None:
        self._redis = redis
        self._identity = identity
        self._lifetime_s = lifetime_s
        self._margin_s = refresh_margin_s
        self._lock_s = refresh_lock_s
        self._clock = clock
        self._credentials = credentials

    async def create(self, tokens: AuthTokens, *, expected_revision: int = 0) -> str:
        session_id = secrets.token_urlsafe(32)
        try:
            if self._credentials:
                state = await self._credentials.snapshot(tokens.user_id)
                if state.blocked or state.revision != expected_revision:
                    raise Unauthenticated()
            version_key = self._version_key(tokens.user_id)
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(version_key)
                version = await pipe.get(version_key)
                key = self._key(session_id)
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.set(key, self._encode(tokens, expected_revision, version), ex=self._lifetime_s)
                pipe.sadd(self._index_key(tokens.user_id), key)
                pipe.expire(self._index_key(tokens.user_id), self._lifetime_s)
                await pipe.execute()
            if self._credentials:
                await self._validate(await self._redis.get(key))
        except WatchError as exc:
            raise Unauthenticated() from exc
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc
        return session_id

    async def revoke_user(self, user_id: UUID) -> None:
        version_key, index_key = self._version_key(user_id), self._index_key(user_id)
        try:
            while True:
                try:
                    async with self._redis.pipeline(transaction=True) as pipe:
                        await pipe.watch(version_key, index_key)
                        keys = await pipe.smembers(index_key)
                        pipe.multi()  # type: ignore[no-untyped-call]
                        pipe.set(version_key, secrets.token_urlsafe(16))
                        if keys:
                            pipe.delete(*keys)
                        pipe.delete(index_key)
                        await pipe.execute()
                    return
                except WatchError:
                    continue
        except (RedisError, OSError) as exc:
            raise DependencyUnavailable() from exc

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
                tokens = await self._validate(raw)
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
                    tokens = await self._validate(raw)
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
                    await self._validate(raw)
                    version_key = self._version_key(tokens.user_id)
                    async with self._redis.pipeline(transaction=True) as pipe:
                        await pipe.watch(key, lock_key, version_key)
                        if await pipe.get(key) != raw:
                            raise Unauthenticated()
                        if await pipe.get(lock_key) != owner.encode():
                            raise DependencyUnavailable()
                        document = json.loads(raw)
                        version = await pipe.get(version_key)
                        if self._revision_value(version) != document.get("user_revision", ""):
                            raise Unauthenticated()
                        pipe.multi()  # type: ignore[no-untyped-call]
                        pipe.set(
                            key,
                            self._encode(
                                refreshed, document.get("credential_revision", 0), version
                            ),
                            keepttl=True,
                        )
                        await pipe.execute()
                    await self._validate(await self._redis.get(key))
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

    def _index_key(self, user_id: UUID) -> str:
        return f"session-user:{user_id}:keys"

    def _version_key(self, user_id: UUID) -> str:
        return f"session-user:{user_id}:revision"

    async def _validate(self, raw: bytes | str | None) -> AuthTokens:
        if raw is None:
            raise Unauthenticated()
        tokens = self._decode(raw)
        document = json.loads(raw)
        version = await self._redis.get(self._version_key(tokens.user_id))
        if self._revision_value(version) != document.get("user_revision", ""):
            raise Unauthenticated()
        if self._credentials:
            state = await self._credentials.snapshot(tokens.user_id)
            if state.blocked or state.revision != document.get("credential_revision", 0):
                raise Unauthenticated()
        return tokens

    def _encode(
        self, tokens: AuthTokens, revision: int = 0, user_revision: bytes | str | None = None
    ) -> str:
        return json.dumps(
            {
                **asdict(tokens),
                "user_id": str(tokens.user_id),
                "credential_revision": revision,
                "user_revision": self._revision_value(user_revision),
            }
        )

    def _revision_value(self, value: bytes | str | None) -> str:
        return value.decode() if isinstance(value, bytes) else value or ""

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
