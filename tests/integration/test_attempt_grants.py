import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg

from nalar.application.errors import Conflict
from nalar.application.features.publications.commands.grant_attempt import (
    AttemptGranted,
    GrantAttempt,
    GrantAttemptHandler,
    GrantTiming,
)
from nalar.application.features.sessions.commands.start_window_session import (
    StartWindowSession,
    StartWindowSessionHandler,
)
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    build_world,
    create_student,
    evaluate,
    finish_session,
)
from tests.integration.test_release import settled
from tests.unit.application.fakes import FakeClock


async def test_grants_require_terminal_attempt_and_valid_window(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    url = f"/publications/{world.publication_id}/attempt-grants"
    body = {"student_id": str(world.student_id), "reason": "Retry"}
    async with api_client(conn, clock=clock) as api:
        active = await api.post(
            url, json=body, headers=as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())}
        )
        assert (active.status_code, active.json()["error"]["code"]) == (
            409,
            "ATTEMPT_NOT_GRANTABLE",
        )
        wrong = await api.post(
            url, json=body, headers=as_user(world.student_id) | {"Idempotency-Key": str(uuid4())}
        )
        assert wrong.status_code == 404
        await settled(conn, world)
        for times in (
            {"opens_at": (clock.now() + timedelta(hours=1)).isoformat()},
            {
                "opens_at": (clock.now() - timedelta(hours=2)).isoformat(),
                "closes_at": (clock.now() - timedelta(hours=1)).isoformat(),
            },
        ):
            invalid = await api.post(
                url,
                json=body | times,
                headers=as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())},
            )
            assert invalid.status_code == 400
        scheduled = await api.post(
            url,
            json=body
            | {
                "opens_at": (clock.now() + timedelta(hours=1)).isoformat(),
                "closes_at": (clock.now() + timedelta(hours=2)).isoformat(),
            },
            headers=as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())},
        )
        assert scheduled.status_code == 201
        run = scheduled.json()["run_id"]
        early = await api.post(
            f"/student/publications/{world.publication_id}/window-session",
            json={"run_id": run},
            headers=as_user(world.student_id),
        )
        assert early.status_code == 409
        await conn.execute(
            "update publication_runs set join_code = 'GRANTX', status = 'open' where id = $1::uuid",
            run,
        )
        bypass = await api.post(
            "/student/runs/join", json={"join_code": "GRANTX"}, headers=as_user(world.student_id)
        )
        assert bypass.status_code == 404


async def test_competing_keys_and_parallel_starts_admit_one_grant_and_one_session(
    pool: asyncpg.Pool,
) -> None:
    async with pool.acquire() as conn:
        world = await build_world(conn, f"Grant race {uuid4().hex[:6]}")
        await settled(conn, world)
    clock, timing = FakeClock(), GrantTiming(timedelta(days=1))
    results = await asyncio.gather(
        *[
            GrantAttemptHandler(PgUnitOfWork(pool.acquire), clock, timing).execute(
                GrantAttempt(
                    world.teacher_id, world.publication_id, world.student_id, "Retry", uuid4()
                )
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    grants = [r for r in results if isinstance(r, AttemptGranted)]
    conflicts = [r for r in results if isinstance(r, Conflict)]
    assert len(grants) == len(conflicts) == 1
    assert conflicts[0].code == "ATTEMPT_NOT_GRANTABLE"
    cmd = StartWindowSession(world.student_id, world.publication_id, grants[0].run_id)
    first, second = await asyncio.gather(
        *[
            StartWindowSessionHandler(PgUnitOfWork(pool.acquire), clock).execute(cmd)
            for _ in range(2)
        ]
    )
    assert first.session_id == second.session_id
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "select count(*) from publication_runs"
                " where publication_id = $1 and kind = 'grant'",
                world.publication_id,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "select attempt_number from sessions where id = $1", first.session_id
            )
            == 2
        )


async def test_grant_retry_scopes_cards_and_starts_exactly_one_attempt(
    conn: asyncpg.Connection, world: World
) -> None:
    await settled(conn, world)
    other = await create_student(conn, world.school_id, world.class_id, world.year_id)
    key = str(uuid4())
    headers = as_user(world.teacher_id) | {"Idempotency-Key": key}
    url = f"/publications/{world.publication_id}/attempt-grants"
    body = {"student_id": str(world.student_id), "reason": "Latihan lagi"}
    async with api_client(conn) as api:
        first = await api.post(url, json=body, headers=headers)
        assert first.status_code == 201, first.text
        duplicate = await api.post(url, json=body, headers=headers)
        assert duplicate.json() == first.json()
        changed = await api.post(url, json=body | {"reason": "Different"}, headers=headers)
        assert (changed.status_code, changed.json()["error"]["code"]) == (
            409,
            "IDEMPOTENCY_CONFLICT",
        )
        another = await api.post(
            url, json=body, headers=as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())}
        )
        assert another.status_code == 409
        run = first.json()["run_id"]
        mine = (await api.get("/student/missions", headers=as_user(world.student_id))).json()
        assert any(
            c["run_id"] == run and c["attempt_number"] == 2 and c["is_granted_attempt"]
            for c in mine["open"]
        )
        assert any(
            c["run_id"] == str(world.run_id) and not c["is_granted_attempt"]
            for c in mine["completed"]
        )
        theirs = (await api.get("/student/missions", headers=as_user(other))).json()
        assert all(c["run_id"] != run for cards in theirs.values() for c in cards)
        start_url = f"/student/publications/{world.publication_id}/window-session"
        wrong = await api.post(start_url, json={"run_id": run}, headers=as_user(other))
        assert wrong.status_code == 404
        started = await api.post(start_url, json={"run_id": run}, headers=as_user(world.student_id))
        assert started.status_code == 201, started.text
        again = await api.post(start_url, json={"run_id": run}, headers=as_user(world.student_id))
        assert again.json() == started.json()
        teacher = as_user(world.teacher_id)
        preview_url = f"/publications/{world.publication_id}/release-preview"
        preview = (await api.get(preview_url, headers=teacher)).json()
        assert preview["ready"] is False
        assert {"code": "RUN_OPEN", "count": 1} in preview["blockers"]
        class_map_url = f"/publications/{world.publication_id}/class-map"
        pending = (await api.get(class_map_url, headers=teacher)).json()
        assert (pending["denominator"], pending["incomplete_count"]) == (0, 1)
        session_id = UUID(started.json()["session_id"])
        await finish_session(conn, world, session_id)
        await evaluate(conn, world, session_id)
        closed = await api.post(f"/runs/{run}/close", headers=teacher)
        assert closed.status_code == 200, closed.text
        assert (await api.get(preview_url, headers=teacher)).json()["ready"] is True
        assert (await api.get(class_map_url, headers=teacher)).json()["denominator"] == 1
    assert (
        await conn.fetchval(
            "select content from session_reflections where session_id = $1", world.session_id
        )
        == "Kamu sudah memikirkan gaya gesek."
    )
    assert (
        await conn.fetchval(
            "select count(*) from parent_summaries where publication_id = $1",
            world.publication_id,
        )
        == 1
    )
    assert (
        await conn.fetchval("select attempt_number from sessions where run_id = $1::uuid", run) == 2
    )
    assert await conn.fetchval("select count(*) from sessions where run_id = $1::uuid", run) == 1
    assert (
        await conn.fetchval(
            "select count(*) from audit_logs where action = 'publication.attempt_granted'"
            " and entity_id = $1",
            world.publication_id,
        )
        == 1
    )


async def test_grant_rejects_released_and_unenrolled_before_lifecycle_errors(
    conn: asyncpg.Connection, world: World
) -> None:
    await settled(conn, world)
    await conn.execute(
        "update publications set released_to_parents_at = now(), released_by = $2 where id = $1",
        world.publication_id,
        world.teacher_id,
    )
    headers = as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())}
    url = f"/publications/{world.publication_id}/attempt-grants"
    async with api_client(conn) as api:
        missing = await api.post(
            url, json={"student_id": str(uuid4()), "reason": "Retry"}, headers=headers
        )
        assert missing.status_code == 404
        released = await api.post(
            url, json={"student_id": str(world.student_id), "reason": "Retry"}, headers=headers
        )
        assert (released.status_code, released.json()["error"]["code"]) == (
            409,
            "PUBLICATION_NOT_GRANTABLE",
        )
