from collections.abc import AsyncIterator

import httpx
from dishka import AsyncContainer, Provider, Scope, make_async_container, provide

from nalar.application.ports.auth import TokenVerifier
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.auth.jwt import SupabaseJwtVerifier


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


def build_container(settings: Settings, *extra: Provider) -> AsyncContainer:
    return make_async_container(InfrastructureProvider(settings), *extra)
