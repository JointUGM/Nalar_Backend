from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg
import fakeredis
import httpx
from dishka import Provider, Scope, provide

from nalar.application.errors import Unauthenticated
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.auth import AuthUser, IdentityProvider, TokenVerifier
from nalar.application.ports.background import BackgroundWork
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.bootstrap.app import create_app
from nalar.bootstrap.container import build_container
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.auth.redis_state import RedisAuthState
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import (
    FakeClock,
    FakeIdentityProvider,
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
    ) -> None:
        super().__init__()
        self._conn = conn
        self._clock = clock
        self._ai = ai
        self._background = background
        self._identity = identity

    @provide(scope=Scope.APP)
    def verifier(self) -> TokenVerifier:
        return UserIdTokens()

    @provide(scope=Scope.APP)
    def identity(self) -> IdentityProvider:
        return self._identity

    @provide(scope=Scope.APP)
    def auth_state(self) -> RedisAuthState:
        return RedisAuthState(fakeredis.FakeAsyncRedis(), 3600, 10)

    @provide(scope=Scope.APP)
    def clock(self) -> Clock:
        return self._clock

    @provide(scope=Scope.APP)
    def ai(self) -> AiGateway:
        return self._ai

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
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings()
    adapters = HarnessAdapters(
        conn,
        clock or FakeClock(),
        ai or ScriptedAiGateway(),
        background or RecordingBackground(),
        identity or FakeIdentityProvider(),
    )
    container = build_container(settings, adapters)
    transport = httpx.ASGITransport(app=create_app(settings, container), raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
            yield http
    finally:
        await container.close()


def as_user(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}
