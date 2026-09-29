from collections.abc import AsyncIterator

import asyncpg
import httpx
from dishka import AsyncContainer, Provider, Scope, make_async_container, provide

from nalar.application.ports.ai import AiGateway
from nalar.application.ports.auth import TokenVerifier
from nalar.application.ports.queue import QueueConsumer
from nalar.application.ports.uow import UnitOfWork
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.ai.client import AiServiceClient, AiTimeouts
from nalar.infrastructure.auth.jwt import SupabaseJwtVerifier
from nalar.infrastructure.db.pool import create_pool
from nalar.infrastructure.db.uow import PgUnitOfWork
from nalar.infrastructure.queue.pgmq import PgmqConsumer


class InfrastructureProvider(Provider):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    @provide(scope=Scope.APP)
    def settings(self) -> Settings:
        return self._settings

    @provide(scope=Scope.APP)
    async def token_verifier(self) -> AsyncIterator[TokenVerifier]:
        secret = self._settings.supabase_jwt_secret
        async with httpx.AsyncClient() as http:
            yield SupabaseJwtVerifier(
                http,
                jwks_url=self._settings.jwks_url,
                audience=self._settings.jwt_audience,
                hs256_secret=secret.get_secret_value() if secret else None,
            )

    @provide(scope=Scope.APP)
    async def pool(self) -> AsyncIterator[asyncpg.Pool]:
        pool = await create_pool(
            self._settings.database_url.get_secret_value(),
            min_size=self._settings.db_pool_min_size,
            max_size=self._settings.db_pool_max_size,
            statement_cache_size=self._settings.db_statement_cache_size,
        )
        yield pool
        await pool.close()

    @provide(scope=Scope.REQUEST)
    def unit_of_work(self, pool: asyncpg.Pool) -> UnitOfWork:
        return PgUnitOfWork(pool.acquire)

    @provide(scope=Scope.APP)
    def queue_consumer(self, pool: asyncpg.Pool) -> QueueConsumer:
        return PgmqConsumer(pool)

    @provide(scope=Scope.APP)
    async def ai_gateway(self) -> AsyncIterator[AiGateway]:
        s = self._settings
        async with httpx.AsyncClient(
            base_url=s.ai_base_url,
            headers={"X-Service-Key": s.ai_service_key.get_secret_value()},
        ) as http:
            yield AiServiceClient(
                http,
                AiTimeouts(
                    turn_s=s.ai_turn_timeout_s,
                    warm_s=s.ai_warm_timeout_s,
                    evaluate_s=s.ai_evaluate_timeout_s,
                    embed_s=s.ai_embed_timeout_s,
                ),
            )


def build_container(settings: Settings, *extra: Provider) -> AsyncContainer:
    return make_async_container(InfrastructureProvider(settings), *extra)
