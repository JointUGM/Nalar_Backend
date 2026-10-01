from uuid import uuid4

import asyncpg

from tests.integration.support.api import api_client
from tests.unit.application.fakes import FakeIdentityProvider

PASSWORD = FakeIdentityProvider.password


async def test_login_returns_metadata_and_an_httponly_session_cookie(
    conn: asyncpg.Connection,
) -> None:
    identity = FakeIdentityProvider()
    async with api_client(conn, identity=identity) as api:
        response = await api.post(
            "/auth/login", json={"email": " Siswa01@Demo.nalar.id ", "password": PASSWORD}
        )
        restored = await api.get("/auth/session")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {"user_id", "expires_at"}
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "SameSite=lax" in response.headers["Set-Cookie"]
    assert restored.json() == response.json()
    assert identity.sign_ins == ["siswa01@demo.nalar.id"]


async def test_wrong_password_is_401_invalid_credentials(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        response = await api.post("/auth/login", json={"email": "a@b.id", "password": "nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_eleventh_login_for_one_email_is_429_whatever_its_case(
    conn: asyncpg.Connection,
) -> None:
    async with api_client(conn) as api:
        for i in range(10):
            await api.post(
                "/auth/login",
                json={
                    "email": "SISWA01@demo.nalar.id" if i % 2 else "siswa01@demo.nalar.id",
                    "password": "nope",
                },
            )
        blocked = await api.post(
            "/auth/login", json={"email": " Siswa01@Demo.Nalar.ID", "password": PASSWORD}
        )
        other = await api.post(
            "/auth/login", json={"email": "siswa02@demo.nalar.id", "password": PASSWORD}
        )
    assert blocked.status_code == 429
    assert other.status_code == 200


async def test_a_class_of_32_can_sign_in_within_one_minute(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        statuses = [
            (
                await api.post(
                    "/auth/login",
                    json={"email": f"siswa{n:02}@demo.nalar.id", "password": PASSWORD},
                )
            ).status_code
            for n in range(1, 33)
        ]
    assert statuses == [200] * 32


async def test_logout_rejects_replayed_cookie_and_is_idempotent(conn: asyncpg.Connection) -> None:
    identity = FakeIdentityProvider()
    async with api_client(conn, identity=identity) as api:
        await api.post("/auth/login", json={"email": "a@b.id", "password": PASSWORD})
        old = api.cookies.get("nalar_session")
        assert (await api.post("/auth/refresh")).status_code == 200
        assert (await api.post("/auth/logout")).status_code == 204
        assert (
            await api.get("/auth/session", headers={"Cookie": f"nalar_session={old}"})
        ).status_code == 401
        assert (await api.post("/auth/logout")).status_code == 204
    assert identity.revoked == ["access-1"]


async def test_bearer_tokens_cannot_bypass_redis_sessions(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        response = await api.get("/me", headers={"Authorization": f"Bearer {uuid4()}"})
    assert response.status_code == 401


async def test_login_rotates_session_and_rejects_previous_cookie(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        await api.post("/auth/login", json={"email": "a@b.id", "password": PASSWORD})
        old = api.cookies.get("nalar_session")
        await api.post("/auth/login", json={"email": "a@b.id", "password": PASSWORD})
        assert api.cookies.get("nalar_session") != old
        assert (
            await api.get("/auth/session", headers={"Cookie": f"nalar_session={old}"})
        ).status_code == 401


async def test_cross_origin_and_missing_csrf_header_cannot_login_or_logout(
    conn: asyncpg.Connection,
) -> None:
    async with api_client(conn) as api:
        response = await api.post(
            "/auth/login",
            headers={"Origin": "https://attacker.test"},
            json={"email": "a@b.id", "password": PASSWORD},
        )
        assert response.status_code == 403
        api.headers.pop("X-Nalar-CSRF")
        assert (await api.post("/auth/logout")).status_code == 403


async def test_cors_only_allows_credentials_for_explicit_origins(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        response = await api.options(
            "/auth/login",
            headers={
                "Origin": "http://test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Nalar-CSRF, Content-Type",
            },
        )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://test"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
