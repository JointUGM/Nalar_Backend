from typing import Any
from uuid import UUID

import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    build_world,
    create_session,
    evaluate,
    finish_session,
    link_parent,
)

SUMMARY = "Ananda menjelaskan gaya gesek dengan contoh dari rumah."


async def summarized(conn: asyncpg.Connection, world: World, *, released: bool) -> None:
    await finish_session(conn, world, world.session_id)
    await evaluate(conn, world, world.session_id)
    await conn.execute(
        "insert into parent_summaries (school_id, publication_id, student_id, content)"
        " values ($1, $2, $3, $4)",
        world.school_id,
        world.publication_id,
        world.student_id,
        SUMMARY,
    )
    if released:
        await conn.execute(
            "update publications set released_to_parents_at = now(), released_by = $2"
            " where id = $1",
            world.publication_id,
            world.teacher_id,
        )


async def get(conn: asyncpg.Connection, user: UUID, path: str) -> dict[str, Any]:
    async with api_client(conn) as api:
        response = await api.get(path, headers=as_user(user))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_children_span_schools(conn: asyncpg.Connection, world: World) -> None:
    other = await build_world(conn, "SMP Kedua")
    await link_parent(conn, other.school_id, world.parent_id, other.student_id)
    body = await get(conn, world.parent_id, "/parent/children")
    assert {(c["student_id"], c["school_name"]) for c in body["items"]} == {
        (str(world.student_id), "SMP Uji"),
        (str(other.student_id), "SMP Kedua"),
    }


async def test_unreleased_publication_leaves_no_trace_for_the_parent(
    conn: asyncpg.Connection, world: World
) -> None:
    await summarized(conn, world, released=False)
    child = f"/parent/children/{world.student_id}"
    async with api_client(conn) as api:
        progress = await api.get(f"{child}/progress", headers=as_user(world.parent_id))
        reflections = await api.get(f"{child}/reflections", headers=as_user(world.parent_id))
    assert progress.json() == {
        "sessions_completed": 0,
        "concepts_understood": [],
        "concepts_developing": [],
        "summaries": [],
    }
    assert reflections.json() == {"items": [], "next_cursor": None}
    for response in (progress, reflections):
        assert "Gaya dan Gerak" not in response.text
        assert str(world.publication_id) not in response.text


async def test_released_result_shows_summary_concepts_and_reflection(
    conn: asyncpg.Connection, world: World
) -> None:
    await summarized(conn, world, released=True)
    child = f"/parent/children/{world.student_id}"
    progress = await get(conn, world.parent_id, f"{child}/progress")
    reflections = await get(conn, world.parent_id, f"{child}/reflections")
    assert progress["sessions_completed"] == 1
    assert progress["concepts_understood"] == ["Gaya gesek"]
    assert progress["summaries"][0]["text"] == SUMMARY
    assert reflections["items"][0]["content"] == "Kamu sudah memikirkan gaya gesek."


async def test_summary_of_a_superseded_attempt_is_hidden(
    conn: asyncpg.Connection, world: World
) -> None:
    await summarized(conn, world, released=True)
    retry = await create_session(
        conn,
        world.school_id,
        world.publication_id,
        world.run_id,
        world.student_id,
        attempt_number=2,
    )
    await finish_session(conn, world, retry, status="timed_out")
    progress = await get(conn, world.parent_id, f"/parent/children/{world.student_id}/progress")
    assert progress["summaries"] == []


async def test_a_parent_of_another_child_gets_404(conn: asyncpg.Connection, world: World) -> None:
    stranger = (await build_world(conn, "SMP Lain")).parent_id
    async with api_client(conn) as api:
        response = await api.get(
            f"/parent/children/{world.student_id}/progress", headers=as_user(stranger)
        )
    assert response.status_code == 404


async def test_preferences_round_trip(conn: asyncpg.Connection, world: World) -> None:
    headers = as_user(world.parent_id)
    async with api_client(conn) as api:
        before = await api.get("/parent/preferences", headers=headers)
        put = await api.put(
            "/parent/preferences", json={"weekly_digest_enabled": False}, headers=headers
        )
        after = await api.get("/parent/preferences", headers=headers)
    assert before.json() == {"weekly_digest_enabled": True}
    assert put.json() == after.json() == {"weekly_digest_enabled": False}
