from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from dishka import Provider, Scope, make_async_container, provide
from fastapi import FastAPI

from nalar.application.errors import (
    AppError,
    Conflict,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    Unauthenticated,
    Unprocessable,
)
from nalar.application.ports.auth import AuthUser, TokenVerifier
from nalar.bootstrap.app import create_app
from nalar.bootstrap.settings import Settings
from nalar.presentation.api.deps import CurrentUser


class FakeVerifier:
    async def verify(self, token: str) -> AuthUser:
        if token != "good":
            raise Unauthenticated()
        return AuthUser(id=UUID(int=1))


class FakeAuthProvider(Provider):
    @provide(scope=Scope.APP)
    def verifier(self) -> TokenVerifier:
        return FakeVerifier()


def build_test_app() -> FastAPI:
    app = create_app(Settings(), make_async_container(FakeAuthProvider()))

    @app.get("/boom/{kind}")
    async def boom(kind: str) -> None:
        errors: dict[str, AppError] = {
            "invalid": InvalidInput(),
            "forbidden": Forbidden("NOT_ASSIGNED_TO_CLASS"),
            "missing": NotFound(),
            "conflict": Conflict("RUN_STATE_CONFLICT", details={"status": "closed"}),
            "unprocessable": Unprocessable("MISSION_VERSION_INVALID"),
            "unavailable": DependencyUnavailable(),
        }
        if kind == "crash":
            raise RuntimeError("secret stack detail")
        raise errors[kind]

    @app.get("/number/{value}")
    async def number(value: int) -> dict[str, int]:
        return {"value": value}

    @app.get("/me")
    async def me(user: CurrentUser) -> dict[str, str]:
        return {"id": str(user.id)}

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_test_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_health_is_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("invalid", 400, "INVALID_INPUT"),
        ("forbidden", 403, "NOT_ASSIGNED_TO_CLASS"),
        ("missing", 404, "NOT_FOUND"),
        ("conflict", 409, "RUN_STATE_CONFLICT"),
        ("unprocessable", 422, "MISSION_VERSION_INVALID"),
        ("unavailable", 503, "DEPENDENCY_UNAVAILABLE"),
    ],
)
async def test_app_errors_map_to_status_and_envelope(
    client: httpx.AsyncClient, kind: str, status: int, code: str
) -> None:
    response = await client.get(f"/boom/{kind}")
    body = response.json()
    assert response.status_code == status
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["request_id"] == response.headers["X-Request-Id"]


async def test_error_details_are_returned(client: httpx.AsyncClient) -> None:
    response = await client.get("/boom/conflict")
    assert response.json()["error"]["details"] == {"status": "closed"}


async def test_validation_error_is_400_invalid_input(client: httpx.AsyncClient) -> None:
    response = await client.get("/number/abc")
    body = response.json()
    assert response.status_code == 400
    assert body["error"]["code"] == "INVALID_INPUT"
    assert body["error"]["details"]["errors"][0]["loc"] == ["path", "value"]


async def test_unknown_route_uses_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_unhandled_exception_is_500_without_internals(client: httpx.AsyncClient) -> None:
    response = await client.get("/boom/crash")
    body = response.json()
    assert response.status_code == 500
    assert body["error"]["code"] == "INTERNAL"
    assert "secret stack detail" not in response.text
    assert body["request_id"] == response.headers["X-Request-Id"]


async def test_valid_incoming_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-Id": "abc-123"})
    assert response.headers["X-Request-Id"] == "abc-123"


async def test_invalid_incoming_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-Id": "bad id with spaces"})
    assert response.headers["X-Request-Id"] != "bad id with spaces"
    assert len(response.headers["X-Request-Id"]) == 32


async def test_missing_bearer_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_rejected_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/me", headers={"Authorization": "Bearer bad"})
    assert response.status_code == 401


async def test_accepted_token_reaches_the_route(client: httpx.AsyncClient) -> None:
    response = await client.get("/me", headers={"Authorization": "Bearer good"})
    assert response.status_code == 200
    assert response.json() == {"id": "00000000-0000-0000-0000-000000000001"}
