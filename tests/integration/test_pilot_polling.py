from uuid import uuid4

import asyncpg
import pytest

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world, open_lobby_run
from tests.unit.application.fakes import FakeClock


@pytest.mark.parametrize("surface", ["state", "lobby", "monitor"])
async def test_polling_revalidates_scope_before_returning_304(
    conn: asyncpg.Connection, world: World, surface: str
) -> None:
    foreign = await build_world(conn, "Other school")
    clock = FakeClock()
    await open_lobby_run(conn, world, f"{uuid4().hex[:6]}")
    await conn.execute(
        "insert into run_participants(school_id,run_id,student_id) values($1,$2,$3)",
        world.school_id,
        world.run_id,
        world.student_id,
    )
    url = {
        "state": f"/student/sessions/{world.session_id}/state",
        "lobby": f"/student/runs/{world.run_id}/lobby",
        "monitor": f"/publications/{world.publication_id}/monitor",
    }[surface]
    actor = world.teacher_id if surface == "monitor" else world.student_id
    outsider = foreign.teacher_id if surface == "monitor" else foreign.student_id
    async with api_client(conn, clock=clock) as api:
        config = await api.get("/config")
        assert config.json() == {
            "password_reset_enabled": False,
            "account_email_enabled": False,
            "weekly_digest_enabled": False,
        }
        first = await api.get(url, headers=as_user(actor))
        assert first.status_code == 200, first.text
        tag = first.headers["etag"]
        clock.advance(seconds=1)
        headers = as_user(actor) | {"If-None-Match": f'"other", {tag}'}
        unchanged = await api.get(url, headers=headers)
        assert unchanged.status_code == 304 and unchanged.content == b""
        assert unchanged.headers["date"] != first.headers["date"]
        assert (
            await api.get(url, headers=as_user(outsider) | {"If-None-Match": tag})
        ).status_code == 404
        await conn.execute("update schools set is_active = false where id = $1", world.school_id)
        assert (await api.get(url, headers=headers)).status_code == 404


async def test_polling_changes_etag_when_visible_state_changes(
    conn: asyncpg.Connection, world: World
) -> None:
    url = f"/student/sessions/{world.session_id}/state"
    async with api_client(conn) as api:
        first = await api.get(url, headers=as_user(world.student_id))
        await conn.execute(
            "update sessions set status = 'paused_safety' where id = $1", world.session_id
        )
        changed = await api.get(
            url,
            headers=as_user(world.student_id) | {"If-None-Match": first.headers["etag"]},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["status"] == "paused_safety"
        assert changed.headers["etag"] != first.headers["etag"]
        preflight = await api.options(
            url,
            headers={
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "If-None-Match",
            },
        )
        assert preflight.status_code == 200
        assert "etag" in changed.headers["access-control-expose-headers"].lower()
