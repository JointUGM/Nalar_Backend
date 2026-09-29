import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from nalar.application.errors import DependencyUnavailable, Unauthenticated
from nalar.infrastructure.auth.jwt import SupabaseJwtVerifier

JWKS_URL = "http://auth.test/auth/v1/.well-known/jwks.json"


class JwksServer:
    def __init__(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.kid = "key-1"
        self.requests = 0
        self.fail = False

    def jwks(self) -> dict[str, Any]:
        public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.private_key.public_key()))
        return {"keys": [{**public, "kid": self.kid, "alg": "ES256", "use": "sig"}]}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        await asyncio.sleep(0)
        if self.fail:
            return httpx.Response(500)
        return httpx.Response(200, json=self.jwks())

    def token(self, **overrides: Any) -> str:
        claims = {
            "sub": str(uuid4()),
            "aud": "authenticated",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            **overrides,
        }
        return jwt.encode(claims, self.private_key, algorithm="ES256", headers={"kid": self.kid})


def unsigned_token(claims: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}."


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_verifier(
    server: JwksServer, clock: FakeClock | None = None, secret: str | None = None
) -> SupabaseJwtVerifier:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return SupabaseJwtVerifier(
        http,
        jwks_url=JWKS_URL,
        audience="authenticated",
        hs256_secret=secret,
        clock=clock or FakeClock(),
    )


async def test_valid_es256_token_yields_the_user() -> None:
    server = JwksServer()
    user_id = uuid4()
    user = await make_verifier(server).verify(server.token(sub=str(user_id)))
    assert user.id == user_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"aud": "anon"},
        {"sub": "not-a-uuid"},
    ],
)
async def test_bad_claims_are_rejected(overrides: dict[str, Any]) -> None:
    server = JwksServer()
    with pytest.raises(Unauthenticated):
        await make_verifier(server).verify(server.token(**overrides))


async def test_token_signed_by_another_key_is_rejected() -> None:
    server = JwksServer()
    forger = JwksServer()
    with pytest.raises(Unauthenticated):
        await make_verifier(server).verify(forger.token())


async def test_garbage_and_unsupported_algorithms_are_rejected() -> None:
    server = JwksServer()
    verifier = make_verifier(server)
    none_token = unsigned_token({"sub": str(uuid4()), "aud": "authenticated"})
    for token in ("not-a-jwt", none_token):
        with pytest.raises(Unauthenticated):
            await verifier.verify(token)


async def test_hs256_token_accepted_only_when_a_secret_is_configured() -> None:
    server = JwksServer()
    secret = "super-secret-jwt-token-with-at-least-32-characters-long"
    claims = {
        "sub": str(uuid4()),
        "aud": "authenticated",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    token = jwt.encode(claims, secret, algorithm="HS256")
    user = await make_verifier(server, secret=secret).verify(token)
    assert str(user.id) == claims["sub"]
    with pytest.raises(Unauthenticated):
        await make_verifier(server).verify(token)


async def test_jwks_is_fetched_once_and_cached() -> None:
    server = JwksServer()
    verifier = make_verifier(server)
    await verifier.verify(server.token())
    await verifier.verify(server.token())
    assert server.requests == 1


async def test_concurrent_first_requests_share_one_jwks_fetch() -> None:
    server = JwksServer()
    verifier = make_verifier(server)
    await asyncio.gather(*(verifier.verify(server.token()) for _ in range(8)))
    assert server.requests == 1


async def test_unknown_kid_refreshes_after_the_cooldown_only() -> None:
    server = JwksServer()
    clock = FakeClock()
    verifier = make_verifier(server, clock)
    await verifier.verify(server.token())
    server.kid = "key-2"
    with pytest.raises(Unauthenticated):
        await verifier.verify(server.token())
    assert server.requests == 1
    clock.now += 61
    await verifier.verify(server.token())
    assert server.requests == 2


async def test_jwks_outage_is_a_dependency_error_not_a_401() -> None:
    server = JwksServer()
    server.fail = True
    verifier = make_verifier(server)
    for _ in range(2):
        with pytest.raises(DependencyUnavailable):
            await verifier.verify(server.token())
    assert server.requests == 1
