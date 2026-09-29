from typing import Any
from uuid import UUID

import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    build_world,
    create_mission_version,
    create_teacher,
)


async def new_version(conn: asyncpg.Connection, world: World, reviewed: bool = True) -> UUID:
    _, version_id = await create_mission_version(
        conn, world.school_id, world.kb_id, world.teacher_id, reviewed=reviewed
    )
    return version_id


def live(version_id: UUID, class_id: UUID) -> dict[str, Any]:
    return {
        "mission_version_id": str(version_id),
        "class_id": str(class_id),
        "run": {"mode": "live"},
    }


async def test_publish_creates_a_scheduled_table_mode_run(
    conn: asyncpg.Connection, world: World
) -> None:
    version_id = await new_version(conn, world)
    async with api_client(conn) as api:
        response = await api.post(
            "/publications",
            json=live(version_id, world.class_id),
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 201
    body = response.json()
    assert body["run_status"] == "scheduled"
    row = await conn.fetchrow(
        "select mode::text, planner_mode::text, join_code from publication_runs where id = $1",
        UUID(body["run_id"]),
    )
    assert row is not None
    assert (row["mode"], row["planner_mode"], row["join_code"]) == ("live", "table", None)


async def test_publishing_twice_returns_the_same_publication(
    conn: asyncpg.Connection, world: World
) -> None:
    version_id = await new_version(conn, world)
    body, headers = live(version_id, world.class_id), as_user(world.teacher_id)
    async with api_client(conn) as api:
        first = await api.post("/publications", json=body, headers=headers)
        second = await api.post("/publications", json=body, headers=headers)
    assert first.json() == second.json()


async def test_unreviewed_version_is_409_version_not_reviewed(
    conn: asyncpg.Connection, world: World
) -> None:
    version_id = await new_version(conn, world, reviewed=False)
    async with api_client(conn) as api:
        response = await api.post(
            "/publications",
            json=live(version_id, world.class_id),
            headers=as_user(world.teacher_id),
        )
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "VERSION_NOT_REVIEWED",
    )


async def test_unassigned_teacher_is_403_not_assigned_to_class(
    conn: asyncpg.Connection, world: World
) -> None:
    version_id = await new_version(conn, world)
    colleague = await create_teacher(conn, world.school_id)
    async with api_client(conn) as api:
        response = await api.post(
            "/publications", json=live(version_id, world.class_id), headers=as_user(colleague)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (
        403,
        "NOT_ASSIGNED_TO_CLASS",
    )


async def test_another_schools_teacher_gets_404(conn: asyncpg.Connection, world: World) -> None:
    version_id = await new_version(conn, world)
    other = await build_world(conn, "SMP Lain")
    async with api_client(conn) as api:
        response = await api.post(
            "/publications",
            json=live(version_id, world.class_id),
            headers=as_user(other.teacher_id),
        )
    assert response.status_code == 404


async def test_window_run_needs_opens_before_closes(conn: asyncpg.Connection, world: World) -> None:
    version_id = await new_version(conn, world)
    body = live(version_id, world.class_id)
    body["run"] = {
        "mode": "window",
        "opens_at": "2026-10-08T03:00:00Z",
        "closes_at": "2026-10-08T02:00:00Z",
    }
    async with api_client(conn) as api:
        response = await api.post("/publications", json=body, headers=as_user(world.teacher_id))
    assert (response.status_code, response.json()["error"]["code"]) == (400, "INVALID_INPUT")
