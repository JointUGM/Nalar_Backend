import asyncio
from uuid import UUID, uuid4

import asyncpg

from nalar.application.features.release.commands.release import Release, ReleaseHandler
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    build_world,
    create_session,
    create_student,
    evaluate,
    finish_session,
)
from tests.unit.application.fakes import FakeClock


async def close_runs(conn: DbConnection, world: World) -> None:
    await conn.execute(
        "update publication_runs set status = 'closed', closed_at = now()"
        " where publication_id = $1",
        world.publication_id,
    )


async def summarize(conn: DbConnection, world: World, student_id: UUID) -> None:
    await conn.execute(
        "insert into parent_summaries (school_id, publication_id, student_id, content)"
        " values ($1, $2, $3, 'Ananda sudah menjelaskan gaya gesek dengan contoh sendiri.')",
        world.school_id,
        world.publication_id,
        student_id,
    )


async def settled(conn: DbConnection, world: World, *, summary: bool = True) -> None:
    await finish_session(conn, world, world.session_id)
    await evaluate(conn, world, world.session_id)
    await close_runs(conn, world)
    if summary:
        await summarize(conn, world, world.student_id)


async def student_session(conn: asyncpg.Connection, world: World) -> UUID:
    student = await create_student(conn, world.school_id, world.class_id, world.year_id)
    return await create_session(conn, world.school_id, world.publication_id, world.run_id, student)


async def test_preview_counts_every_blocker(conn: asyncpg.Connection, world: World) -> None:
    unevaluated = await student_session(conn, world)
    await finish_session(conn, world, unevaluated)
    unsummarized = await student_session(conn, world)
    await finish_session(conn, world, unsummarized)
    await evaluate(conn, world, unsummarized)
    async with api_client(conn) as api:
        response = await api.get(
            f"/publications/{world.publication_id}/release-preview",
            headers=as_user(world.teacher_id),
        )
    body = response.json()
    assert body["ready"] is False
    assert {(b["code"], b["count"]) for b in body["blockers"]} == {
        ("RUN_OPEN", 1),
        ("SESSION_ACTIVE", 1),
        ("EVALUATION_PENDING", 1),
        ("SUMMARIES_PENDING", 1),
    }
    assert body["eligible_count"] == 1


async def test_timed_out_only_class_has_no_eligible_result(
    conn: asyncpg.Connection, world: World
) -> None:
    await finish_session(conn, world, world.session_id, status="timed_out")
    await evaluate(conn, world, world.session_id)
    await close_runs(conn, world)
    async with api_client(conn) as api:
        body = (
            await api.get(
                f"/publications/{world.publication_id}/release-preview",
                headers=as_user(world.teacher_id),
            )
        ).json()
    assert body["blockers"] == [{"code": "NO_ELIGIBLE_RESULT", "count": 0}]
    assert (body["eligible_count"], body["ineligible_count"]) == (0, 1)


async def test_release_is_audited_and_idempotent(conn: asyncpg.Connection, world: World) -> None:
    await settled(conn, world)
    clock = FakeClock()
    headers = as_user(world.teacher_id)
    async with api_client(conn, clock=clock) as api:
        preview = await api.get(
            f"/publications/{world.publication_id}/release-preview", headers=headers
        )
        first = await api.post(
            f"/publications/{world.publication_id}/release",
            json={"expected_eligible_count": 1},
            headers=headers,
        )
        clock.advance(minutes=5)
        again = await api.post(
            f"/publications/{world.publication_id}/release",
            json={"expected_eligible_count": 1},
            headers=headers,
        )
    assert preview.json()["summaries"][0]["summary_text"].startswith("Ananda")
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert first.json()["summary_count"] == 1
    assert (
        await conn.fetchval(
            "select count(*) from audit_logs"
            " where action = 'publication.released' and entity_id = $1",
            world.publication_id,
        )
        == 1
    )


async def test_release_with_stale_count_returns_409(conn: asyncpg.Connection, world: World) -> None:
    await settled(conn, world)
    async with api_client(conn) as api:
        response = await api.post(
            f"/publications/{world.publication_id}/release",
            json={"expected_eligible_count": 2},
            headers=as_user(world.teacher_id),
        )
    assert (response.status_code, response.json()["error"]["code"]) == (409, "RELEASE_NOT_READY")
    assert (
        await conn.fetchval(
            "select released_to_parents_at from publications where id = $1", world.publication_id
        )
        is None
    )


async def test_concurrent_release_happens_once(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as c:
        world = await build_world(c, f"SMP Rilis {uuid4().hex[:6]}")
        await settled(c, world)
    command = Release(world.teacher_id, world.publication_id, 1)
    a, b = await asyncio.gather(
        ReleaseHandler(PgUnitOfWork(pool.acquire), FakeClock()).execute(command),
        ReleaseHandler(PgUnitOfWork(pool.acquire), FakeClock()).execute(command),
    )
    assert a.released_to_parents_at == b.released_to_parents_at
    async with pool.acquire() as c:
        assert (
            await c.fetchval(
                "select count(*) from audit_logs where action = 'publication.released'"
                " and entity_id = $1",
                world.publication_id,
            )
            == 1
        )
