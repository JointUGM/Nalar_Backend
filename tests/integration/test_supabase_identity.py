from collections.abc import AsyncIterator

import httpx
import pytest

from nalar.application.errors import InvalidCredentials, Unauthenticated
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.auth.admin import SupabaseAuthAdmin
from nalar.infrastructure.auth.gotrue import SupabaseIdentityProvider
from nalar.infrastructure.auth.jwt import SupabaseJwtVerifier

EMAIL = "auth-flow@test.nalar.id"
PASSWORD = "auth-flow-password-1"


@pytest.fixture
async def provider() -> AsyncIterator[SupabaseIdentityProvider]:
    s = Settings()
    anon = s.supabase_anon_key.get_secret_value()
    service = s.supabase_service_role_key.get_secret_value()
    if not anon or not service:
        pytest.skip("needs the local stack's keys: npx supabase status -o env")
    admin_headers = {"apikey": service, "Authorization": f"Bearer {service}"}
    async with httpx.AsyncClient(base_url=s.supabase_url, headers=admin_headers) as admin:
        await SupabaseAuthAdmin(admin).ensure_user(EMAIL, PASSWORD, "Auth Flow")
    async with httpx.AsyncClient(
        base_url=s.supabase_url, headers={"apikey": anon}, timeout=5.0
    ) as http:
        yield SupabaseIdentityProvider(http)


async def test_login_refresh_and_logout_round_trip_on_the_real_auth_server(
    provider: SupabaseIdentityProvider,
) -> None:
    s = Settings()
    tokens = await provider.sign_in(EMAIL, PASSWORD)
    secret = s.supabase_jwt_secret
    async with httpx.AsyncClient() as http:
        verifier = SupabaseJwtVerifier(
            http, s.jwks_url, s.jwt_audience, secret.get_secret_value() if secret else None
        )
        user = await verifier.verify(tokens.access_token)
    assert user.id == tokens.user_id
    assert user.session_id is not None

    rotated = await provider.refresh(tokens.refresh_token)
    assert rotated.user_id == tokens.user_id
    assert rotated.refresh_token != tokens.refresh_token

    await provider.sign_out(rotated.access_token)
    with pytest.raises(Unauthenticated):
        await provider.refresh(rotated.refresh_token)


async def test_wrong_password_on_the_real_auth_server_is_invalid_credentials(
    provider: SupabaseIdentityProvider,
) -> None:
    with pytest.raises(InvalidCredentials):
        await provider.sign_in(EMAIL, "not-the-password")
