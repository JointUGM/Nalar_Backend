from datetime import timedelta

import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world, create_run, publish
from tests.unit.application.fakes import FakeClock


async def test_window_edits_are_scoped_and_stop_at_opening_time(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    publication = await publish(
        conn, world.school_id, world.version_id, world.class_id, world.teacher_id
    )
    run = await create_run(conn, world.school_id, publication, mode="window")
    await conn.execute(
        "update publication_runs set opens_at = $2, closes_at = $3 where id = $1",
        run,
        clock.now() + timedelta(days=1),
        clock.now() + timedelta(days=2),
    )
    foreign = await build_world(conn, "Other school")
    body = {
        "opens_at": (clock.now() + timedelta(hours=1)).isoformat(),
        "closes_at": (clock.now() + timedelta(hours=2)).isoformat(),
    }
    url = f"/publications/{publication}"
    async with api_client(conn, clock=clock) as api:
        assert (await api.get(url, headers=as_user(foreign.teacher_id))).status_code == 404
        assert (
            await api.patch(url, json=body, headers=as_user(world.student_id))
        ).status_code == 404
        changed = await api.patch(url, json=body, headers=as_user(world.teacher_id))
        assert changed.status_code == 204, changed.text
        detail = await api.get(url, headers=as_user(world.teacher_id))
        assert detail.json()["runs"][0]["opens_at"].startswith(body["opens_at"][:19])
        clock.advance(hours=1)
        assert (
            await api.patch(url, json=body, headers=as_user(world.teacher_id))
        ).status_code == 409


async def test_cancellation_is_idempotent_and_rejects_any_existing_session(
    conn: asyncpg.Connection, world: World
) -> None:
    publication = await publish(
        conn, world.school_id, world.version_id, world.class_id, world.teacher_id
    )
    run = await create_run(conn, world.school_id, publication, mode="window", status="open")
    async with api_client(conn) as api:
        headers = as_user(world.teacher_id)
        for _ in range(2):
            cancelled = await api.post(f"/publications/{publication}/cancel", headers=headers)
            assert cancelled.status_code == 204, cancelled.text
        assert (
            await conn.fetchval("select status::text from publication_runs where id = $1", run)
            == "closed"
        )
        assert (await api.get(f"/publications/{publication}", headers=headers)).json()[
            "cancelled_at"
        ]
        denied = await api.post(f"/publications/{world.publication_id}/cancel", headers=headers)
        assert (
            denied.status_code == 409
            and denied.json()["error"]["code"] == "PUBLICATION_HAS_SESSIONS"
        )
        closed = await api.post(f"/runs/{run}/close", headers=headers)
        assert closed.status_code == 200, closed.text


async def test_class_map_edges_only_connect_the_publication_target_concepts(
    conn: asyncpg.Connection, world: World
) -> None:
    foreign = await build_world(conn, "Other school")
    a, b = world.concept_ids[:2]
    await conn.execute(
        "insert into concept_prerequisites(knowledge_base_id,concept_id,prerequisite_concept_id)"
        " values($1,$2,$3) on conflict do nothing",
        world.kb_id,
        a,
        b,
    )
    async with api_client(conn) as api:
        result = await api.get(
            f"/publications/{world.publication_id}/class-map", headers=as_user(world.teacher_id)
        )
        assert result.status_code == 200, result.text
        assert {"concept_id": str(a), "prerequisite_id": str(b)} in result.json()["prerequisites"]
        denied = await api.get(
            f"/publications/{world.publication_id}/class-map", headers=as_user(foreign.teacher_id)
        )
        assert denied.status_code == 404


async def test_closing_an_open_window_stops_entry_and_keeps_started_sessions(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "update publication_runs set mode = 'window', status = 'open', opens_at = now(),"
        " closes_at = now() + interval '1 hour' where id = $1",
        world.run_id,
    )
    async with api_client(conn) as api:
        response = await api.post(f"/runs/{world.run_id}/close", headers=as_user(world.teacher_id))
        assert response.status_code == 200, response.text
        assert (
            await conn.fetchval(
                "select status::text from sessions where id = $1",
                world.session_id,
            )
            == "in_progress"
        )
        assert (
            await conn.fetchval(
                "select status::text from publication_runs where id = $1",
                world.run_id,
            )
            == "closed"
        )
