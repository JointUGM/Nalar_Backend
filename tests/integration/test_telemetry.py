from typing import Any

import asyncpg
import pytest

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world

AT = "2026-10-08T02:00:00Z"


def batch(seq: int, events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"client_seq": seq, "turn_index": 0, "events": events}


def url(world: World) -> str:
    return f"/student/sessions/{world.session_id}/telemetry"


async def test_a_duplicate_client_seq_is_accepted_once(
    conn: asyncpg.Connection, world: World
) -> None:
    body = batch(
        1,
        [
            {"type": "paste", "at": AT, "value": 120},
            {"type": "typing", "at": AT, "value": {"duration_ms": 900, "chars": 40}},
        ],
    )
    async with api_client(conn) as api:
        first = await api.post(url(world), json=body, headers=as_user(world.student_id))
        second = await api.post(url(world), json=body, headers=as_user(world.student_id))
    rows = await conn.fetch(
        "select turn_id from telemetry_batches where session_id = $1", world.session_id
    )
    assert first.json() == second.json() == {"accepted_client_seq": 1}
    assert len(rows) == 1 and rows[0]["turn_id"] is not None


@pytest.mark.parametrize(
    ("seq", "events"),
    [
        (2, [{"type": "keystroke", "at": AT}]),
        (2, [{"type": "paste", "at": AT, "value": "isi yang ditempel"}]),
        (2, [{"type": "typing", "at": AT, "value": {"duration_ms": 1, "chars": 1, "text": "x"}}]),
        (2, [{"type": "disconnect", "at": AT}] * 201),
        (2**31, [{"type": "disconnect", "at": AT}]),
    ],
    ids=["unknown-type", "string-value", "extra-key", "too-many", "seq-overflow"],
)
async def test_bad_batches_are_400(
    conn: asyncpg.Connection, world: World, seq: int, events: list[dict[str, Any]]
) -> None:
    async with api_client(conn) as api:
        response = await api.post(
            url(world), json=batch(seq, events), headers=as_user(world.student_id)
        )
    assert response.status_code == 400


async def test_another_students_session_is_404(conn: asyncpg.Connection, world: World) -> None:
    other = await build_world(conn, "SMP Lain")
    body = batch(1, [{"type": "disconnect", "at": AT}])
    async with api_client(conn) as api:
        response = await api.post(url(world), json=body, headers=as_user(other.student_id))
    assert response.status_code == 404
