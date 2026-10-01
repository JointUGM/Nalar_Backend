from uuid import uuid4

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from nalar.application.errors import DependencyUnavailable
from nalar.infrastructure.auth.redis_state import RedisAuthState


def state(now: list[float]) -> tuple[RedisAuthState, fakeredis.FakeAsyncRedis]:
    redis = fakeredis.FakeAsyncRedis()
    return RedisAuthState(redis, 3600, 10, clock=lambda: now[0]), redis


async def test_a_revoked_session_stays_revoked_for_a_full_token_lifetime() -> None:
    store, redis = state([120.0])
    session = uuid4()
    assert not await store.is_revoked(session)
    await store.revoke(session)
    assert await store.is_revoked(session)
    assert await redis.ttl(f"revoked:{session}") >= 3600


async def test_the_eleventh_attempt_in_a_minute_is_refused_until_the_next_minute() -> None:
    now = [120.0]
    store, _ = state(now)
    assert [await store.allow("a@b.id") for _ in range(11)] == [True] * 10 + [False]
    assert await store.allow("c@d.id")
    now[0] = 180.0
    assert await store.allow("a@b.id")


async def test_login_keys_never_contain_the_email() -> None:
    store, redis = state([120.0])
    await store.allow("siswa01@demo.nalar.id")
    keys = await redis.keys("*")
    assert keys
    assert all(b"siswa01" not in key for key in keys)


async def test_unreachable_redis_fails_closed_and_backs_off() -> None:
    calls = 0

    class Down:
        async def exists(self, *_: object) -> int:
            nonlocal calls
            calls += 1
            raise RedisConnectionError("down")

    now = [0.0]
    store = RedisAuthState(Down(), 3600, 10, clock=lambda: now[0])  # type: ignore[arg-type]
    with pytest.raises(DependencyUnavailable):
        await store.is_revoked(uuid4())
    with pytest.raises(DependencyUnavailable):
        await store.is_revoked(uuid4())
    assert calls == 1
    now[0] = 6.0
    with pytest.raises(DependencyUnavailable):
        await store.is_revoked(uuid4())
    assert calls == 2
