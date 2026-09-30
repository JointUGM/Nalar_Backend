from datetime import datetime
from uuid import UUID

import asyncpg
import pytest

from nalar.domain import join_code
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, add_waiting_students, open_lobby_run
from tests.unit.application.fakes import FakeClock


async def screen(conn: asyncpg.Connection, world: World, name: str) -> tuple[str, UUID]:
    if name in ("lobby", "closed_lobby"):
        await open_lobby_run(conn, world, join_code.generate())
        (waiting,) = await add_waiting_students(conn, world, 1)
        if name == "closed_lobby":
            await conn.execute(
                "update run_participants set status = 'cancelled' where run_id = $1", world.run_id
            )
            await conn.execute(
                "update publication_runs set status = 'closed', closed_at = now() where id = $1",
                world.run_id,
            )
        return f"/student/runs/{world.run_id}/lobby", waiting
    if name == "state":
        return f"/student/sessions/{world.session_id}/state", world.student_id
    return f"/publications/{world.publication_id}/monitor", world.teacher_id


@pytest.mark.parametrize("name", ["monitor", "lobby", "closed_lobby", "state"])
async def test_polled_screen_reports_the_server_clock(
    conn: asyncpg.Connection, world: World, name: str
) -> None:
    path, user = await screen(conn, world, name)
    clock = FakeClock()
    async with api_client(conn, clock=clock) as api:
        response = await api.get(path, headers=as_user(user))
    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["server_now"]) == clock.now()
    if name == "closed_lobby":
        assert response.json()["participant_status"] == "cancelled"
