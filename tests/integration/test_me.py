import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World


async def test_me_lists_active_roles_and_the_parent_flag(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        teacher = (await api.get("/me", headers=as_user(world.teacher_id))).json()
        parent = (await api.get("/me", headers=as_user(world.parent_id))).json()
    assert [r["role"] for r in teacher["roles"]] == ["teacher"]
    assert teacher["is_parent"] is False
    assert parent["roles"] == []
    assert parent["is_parent"] is True


async def test_me_without_a_token_is_401(conn: asyncpg.Connection) -> None:
    async with api_client(conn) as api:
        assert (await api.get("/me")).status_code == 401
