from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg
import fakeredis
import httpx
from dishka import Provider, Scope, provide

from nalar.application.errors import Unauthenticated
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.auth import (
    AuthUser,
    BrowserSessions,
    IdentityProvider,
    TokenVerifier,
)
from nalar.application.ports.background import BackgroundWork
from nalar.application.ports.clock import Clock
from nalar.application.ports.storage import ObjectStorage
from nalar.application.ports.uow import UnitOfWork
from nalar.bootstrap.app import create_app
from nalar.bootstrap.container import build_container
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.auth.redis_sessions import RedisBrowserSessions
from nalar.infrastructure.auth.redis_state import RedisAuthState
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import (
    FakeClock,
    FakeIdentityProvider,
    FakeStorage,
    RecordingBackground,
    ScriptedAiGateway,
)


class UserIdTokens:
    async def verify(self, token: str) -> AuthUser:
        user, _, session = token.partition(":")
        try:
            return AuthUser(id=UUID(user), session_id=UUID(session) if session else None)
        except ValueError as exc:
            raise Unauthenticated() from exc


class HarnessAdapters(Provider):
    def __init__(
        self,
        conn: asyncpg.Connection,
        clock: FakeClock,
        ai: ScriptedAiGateway,
        background: RecordingBackground,
        identity: FakeIdentityProvider,
        storage: FakeStorage,
    ) -> None:
        super().__init__()
        self._storage = storage
        self._conn = conn
        self._clock = clock
        self._ai = ai
        self._background = background
        self._identity = identity
        self._redis = fakeredis.FakeAsyncRedis()

    @provide(scope=Scope.APP)
    def verifier(self) -> TokenVerifier:
        return UserIdTokens()

    @provide(scope=Scope.APP)
    def identity(self) -> IdentityProvider:
        return self._identity

    @provide(scope=Scope.APP)
    def auth_state(self) -> RedisAuthState:
        return RedisAuthState(self._redis, 3600, 10)

    @provide(scope=Scope.APP)
    def sessions(self) -> BrowserSessions:
        return RedisBrowserSessions(self._redis, self._identity, 43200, 60, 15)

    @provide(scope=Scope.APP)
    def clock(self) -> Clock:
        return self._clock

    @provide(scope=Scope.APP)
    def ai(self) -> AiGateway:
        return self._ai

    @provide(scope=Scope.APP)
    def storage(self) -> ObjectStorage:
        return self._storage

    @provide(scope=Scope.APP)
    def background(self) -> BackgroundWork:
        return self._background

    @provide(scope=Scope.REQUEST)
    def uow(self) -> UnitOfWork:
        return uow_on(self._conn)


@asynccontextmanager
async def api_client(
    conn: asyncpg.Connection,
    clock: FakeClock | None = None,
    ai: ScriptedAiGateway | None = None,
    background: RecordingBackground | None = None,
    identity: FakeIdentityProvider | None = None,
    storage: FakeStorage | None = None,
    providers: tuple[Provider, ...] = (),
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(env="test", cors_origins=["http://test"])
    adapters = HarnessAdapters(
        conn,
        clock or FakeClock(),
        ai or ScriptedAiGateway(),
        background or RecordingBackground(),
        identity or FakeIdentityProvider(),
        storage or FakeStorage(),
    )
    container = build_container(settings, adapters, *providers)
    transport = httpx.ASGITransport(app=create_app(settings, container), raise_app_exceptions=False)
    try:

        async def seed_test_session(request: httpx.Request) -> None:
            import hashlib
            import json
            from http.cookies import SimpleCookie

            cookie = SimpleCookie()
            cookie.load(request.headers.get("Cookie", ""))
            value = cookie.get("nalar_session")
            if value and value.value.startswith("test-"):
                user_id = UUID(value.value[5:])
                await adapters._redis.set(
                    "session:" + hashlib.sha256(value.value.encode()).hexdigest(),
                    json.dumps(
                        {
                            "user_id": str(user_id),
                            "access_token": "test",
                            "refresh_token": "test",
                            "expires_at": 1900000000,
                        }
                    ),
                    ex=43200,
                )

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test/api/v1",
            headers={"Origin": "http://test", "X-Nalar-CSRF": "1"},
            event_hooks={"request": [seed_test_session]},
        ) as http:
            yield http
    finally:
        await container.close()


def as_user(user_id: UUID) -> dict[str, str]:
    return {"Cookie": f"nalar_session=test-{user_id}"}
