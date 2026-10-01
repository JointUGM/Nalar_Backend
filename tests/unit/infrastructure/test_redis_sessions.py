import asyncio
import hashlib
from uuid import uuid4

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from nalar.application.errors import DependencyUnavailable, Unauthenticated
from nalar.application.ports.auth import AuthTokens
from nalar.infrastructure.auth.redis_sessions import RedisBrowserSessions
from tests.unit.application.fakes import FakeIdentityProvider


def store(
    identity: FakeIdentityProvider | None = None,
) -> tuple[RedisBrowserSessions, fakeredis.FakeAsyncRedis]:
    redis = fakeredis.FakeAsyncRedis()
    return RedisBrowserSessions(
        redis, identity or FakeIdentityProvider(), 43200, 60, 15, clock=lambda: 1000
    ), redis


async def test_session_uses_a_random_identifier_and_expiring_hashed_redis_key() -> None:
    sessions, redis = store()
    tokens = AuthTokens(uuid4(), "access", "refresh", 2000)
    session_id = await sessions.create(tokens)
    assert len(session_id) >= 43
    key = "session:" + hashlib.sha256(session_id.encode()).hexdigest()
    assert await redis.ttl(key) > 43190
    assert session_id.encode() not in key.encode()
    assert await sessions.resolve(session_id) == tokens
    await sessions.delete(session_id)
    with pytest.raises(Unauthenticated):
        await sessions.resolve(session_id)


async def test_concurrent_requests_refresh_once_without_extending_session_lifetime() -> None:
    identity = FakeIdentityProvider()
    sessions, redis = store(identity)
    tokens = await identity.sign_in("a@b.id", identity.password)
    session_id = await sessions.create(
        AuthTokens(tokens.user_id, tokens.access_token, tokens.refresh_token, 900)
    )
    results = await asyncio.gather(*(sessions.resolve(session_id) for _ in range(8)))
    assert {result.refresh_token for result in results} == {"refresh-2"}
    assert identity._issued == 2
    assert await redis.ttl("session:" + hashlib.sha256(session_id.encode()).hexdigest()) <= 43200


async def test_logout_during_refresh_cannot_restore_the_session() -> None:
    started, resume = asyncio.Event(), asyncio.Event()

    class PausedIdentity(FakeIdentityProvider):
        async def refresh(self, refresh_token: str) -> AuthTokens:
            started.set()
            await resume.wait()
            return await super().refresh(refresh_token)

    identity = PausedIdentity()
    sessions, _ = store(identity)
    tokens = await identity.sign_in("a@b.id", identity.password)
    session_id = await sessions.create(
        AuthTokens(tokens.user_id, tokens.access_token, tokens.refresh_token, 900)
    )
    refreshing = asyncio.create_task(sessions.resolve(session_id))
    await started.wait()
    await sessions.delete(session_id)
    resume.set()
    with pytest.raises(Unauthenticated):
        await refreshing
    with pytest.raises(Unauthenticated):
        await sessions.resolve(session_id)


async def test_expired_redis_session_is_rejected_even_with_a_fresh_access_token() -> None:
    sessions, redis = store()
    session_id = await sessions.create(AuthTokens(uuid4(), "access", "refresh", 2000))
    await redis.expire("session:" + hashlib.sha256(session_id.encode()).hexdigest(), 0)
    with pytest.raises(Unauthenticated):
        await sessions.resolve(session_id)


async def test_redis_outage_never_authenticates_or_creates_a_session() -> None:
    class Down:
        async def get(self, key: str) -> bytes:
            raise RedisConnectionError("down")

        async def set(self, *args: object, **kwargs: object) -> None:
            raise RedisConnectionError("down")

    sessions = RedisBrowserSessions(Down(), FakeIdentityProvider(), 43200, 60, 15)  # type: ignore[arg-type]
    with pytest.raises(DependencyUnavailable):
        await sessions.resolve("opaque")
    with pytest.raises(DependencyUnavailable):
        await sessions.create(AuthTokens(uuid4(), "a", "r", 2000))
