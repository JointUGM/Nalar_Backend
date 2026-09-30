from uuid import uuid4

import asyncpg

from tests.integration.support.api import api_client
from tests.integration.support.factories import World
from tests.unit.application.fakes import FakeIdentityProvider

PASSWORD = FakeIdentityProvider.password


async def test_login_returns_only_the_session_fields_and_is_not_cached(
    conn: asyncpg.Connection,
) -> None:
    identity = FakeIdentityProvider()
    async with api_client(conn, identity=identity) as api:
        response = await api.post(
            "/auth/login", json={"email": " Siswa01@Demo.nalar.id ", "password": PASSWORD}
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {
        "user_id",
        "access_token",
        "refresh_token",
        "token_type",
        "expires_at",
    }
    assert identity.sign_ins == ["siswa01@demo.nalar.id"]


async def test_wrong_password_is_401_invalid_credentials(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        response = await api.post("/auth/login", json={"email": "a@b.id", "password": "nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_eleventh_login_for_one_email_in_a_minute_is_429_whatever_its_case(
    conn: asyncpg.Connection,
) -> None:
    async with api_client(conn) as api:
        for i in range(10):
            email = "SISWA01@demo.nalar.id" if i % 2 else "siswa01@demo.nalar.id"
            await api.post("/auth/login", json={"email": email, "password": "nope"})
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


async def test_used_refresh_token_is_401(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        session = (
            await api.post("/auth/login", json={"email": "a@b.id", "password": PASSWORD})
        ).json()
        first = await api.post("/auth/refresh", json={"refresh_token": session["refresh_token"]})
        replay = await api.post("/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert first.status_code == 200
    assert first.json()["refresh_token"] != session["refresh_token"]
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_a_logged_out_token_is_rejected_on_the_next_request(
    conn: asyncpg.Connection, world: World
) -> None:
    identity = FakeIdentityProvider()
    token = f"{world.teacher_id}:{uuid4()}"
    bearer = {"Authorization": f"Bearer {token}"}
    async with api_client(conn, identity=identity) as api:
        before = await api.get("/me", headers=bearer)
        logout = await api.post("/auth/logout", headers=bearer)
        after = await api.get("/me", headers=bearer)
    assert (before.status_code, logout.status_code, after.status_code) == (200, 204, 401)
    assert identity.revoked == [token]


async def test_logout_with_a_dead_token_is_204_and_revokes_nothing(
    conn: asyncpg.Connection,
) -> None:
    identity = FakeIdentityProvider()
    async with api_client(conn, identity=identity) as api:
        response = await api.post("/auth/logout", headers={"Authorization": "Bearer expired"})
    assert response.status_code == 204
    assert identity.revoked == []


async def test_logout_without_a_bearer_is_401(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        assert (await api.post("/auth/logout")).status_code == 401
