import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from nalar.application.errors import (
    DependencyUnavailable,
    InvalidActivation,
    InvalidCredentials,
    TooManyRequests,
    Unauthenticated,
)
from nalar.infrastructure.auth.gotrue import SupabaseIdentityProvider

USER_ID = uuid4()


def session_body() -> dict[str, Any]:
    return {
        "access_token": "access-1",
        "token_type": "bearer",
        "expires_in": 3600,
        "expires_at": 1_900_000_000,
        "refresh_token": "refresh-1",
        "user": {"id": str(USER_ID), "user_metadata": {"full_name": "Siswa"}},
    }


def provider(handler: Callable[[httpx.Request], httpx.Response]) -> SupabaseIdentityProvider:
    transport = httpx.MockTransport(handler)
    return SupabaseIdentityProvider(
        httpx.AsyncClient(transport=transport, base_url="http://auth.test")
    )


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("slow", request=request)


async def test_global_logout_uses_global_scope_and_cannot_silently_accept_rejected_token() -> None:
    seen: list[httpx.Request] = []

    def accepted(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    await provider(accepted).sign_out_all("temporary-access")
    assert seen[0].url.params["scope"] == "global"
    assert seen[0].headers["Authorization"] == "Bearer temporary-access"
    with pytest.raises(DependencyUnavailable):
        await provider(lambda _: httpx.Response(401)).sign_out_all("rejected-access")


async def test_sign_in_sends_the_password_grant_and_returns_only_tokens() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=session_body())

    tokens = await provider(handler).sign_in("siswa01@demo.nalar.id", "pw")

    assert seen[0].url.path == "/auth/v1/token"
    assert seen[0].url.params["grant_type"] == "password"
    assert json.loads(seen[0].content) == {"email": "siswa01@demo.nalar.id", "password": "pw"}
    assert (tokens.user_id, tokens.access_token, tokens.refresh_token, tokens.expires_at) == (
        USER_ID,
        "access-1",
        "refresh-1",
        1_900_000_000,
    )


async def test_wrong_password_is_invalid_credentials() -> None:
    body = {"code": 400, "error_code": "invalid_credentials", "msg": "Invalid login credentials"}
    with pytest.raises(InvalidCredentials):
        await provider(lambda _: httpx.Response(400, json=body)).sign_in("a@b.id", "wrong")


@pytest.mark.parametrize(
    "respond",
    [lambda _: httpx.Response(500), timeout],
    ids=["auth-500", "auth-timeout"],
)
async def test_auth_outage_is_dependency_unavailable_not_invalid_credentials(
    respond: Callable[[httpx.Request], httpx.Response],
) -> None:
    with pytest.raises(DependencyUnavailable):
        await provider(respond).sign_in("a@b.id", "pw")


async def test_rejected_apikey_is_dependency_unavailable_not_invalid_credentials() -> None:
    with pytest.raises(DependencyUnavailable):
        await provider(lambda _: httpx.Response(401)).sign_in("a@b.id", "pw")


async def test_auth_rate_limit_is_too_many_requests() -> None:
    with pytest.raises(TooManyRequests):
        await provider(lambda _: httpx.Response(429)).sign_in("a@b.id", "pw")


async def test_malformed_success_body_is_dependency_unavailable() -> None:
    broken = {**session_body(), "access_token": None}
    with pytest.raises(DependencyUnavailable):
        await provider(lambda _: httpx.Response(200, json=broken)).sign_in("a@b.id", "pw")


async def test_rejected_refresh_token_is_unauthenticated() -> None:
    body = {"code": 400, "error_code": "refresh_token_already_used"}
    with pytest.raises(Unauthenticated):
        await provider(lambda _: httpx.Response(400, json=body)).refresh("refresh-0")


async def test_sign_out_with_an_expired_token_succeeds() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    await provider(handler).sign_out("expired-access")

    assert seen[0].url.path == "/auth/v1/logout"
    assert seen[0].url.params["scope"] == "local"
    assert seen[0].headers["Authorization"] == "Bearer expired-access"


async def test_recovery_proof_updates_password_with_recipient_bearer_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/verify"):
            return httpx.Response(
                200,
                json={
                    **session_body(),
                    "user": {"id": str(USER_ID), "email": "recipient@test.nalar"},
                },
            )
        return httpx.Response(200, json={"id": str(USER_ID)})

    auth = provider(handler)
    proof = await auth.verify_recovery("one-time-proof")
    await auth.update_password(proof, "new-password")
    assert json.loads(seen[0].content) == {"type": "recovery", "token_hash": "one-time-proof"}
    assert seen[1].method == "PUT" and seen[1].url.path == "/auth/v1/user"
    assert seen[1].headers["Authorization"] == "Bearer access-1"
    assert json.loads(seen[1].content) == {"password": "new-password"}
    assert "access-1" not in repr(proof)


async def test_invalid_recovery_proof_is_generic_and_provider_outage_is_503() -> None:
    with pytest.raises(InvalidActivation):
        await provider(lambda _: httpx.Response(403, json={"msg": "secret"})).verify_recovery(
            "proof"
        )
    with pytest.raises(DependencyUnavailable):
        await provider(timeout).verify_recovery("proof")


async def test_malformed_recovery_session_is_rejected_without_logging_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(DependencyUnavailable):
        await provider(
            lambda _: httpx.Response(
                200,
                json={
                    "access_token": "secret-token",
                    "user": {"id": "secret-user", "email": "recipient@test.nalar"},
                },
            )
        ).verify_recovery("proof")
    assert "secret-token" not in caplog.text and "secret-user" not in caplog.text
