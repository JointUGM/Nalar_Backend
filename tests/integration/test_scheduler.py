import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg

from nalar.application.features.scheduler.commands.tick import SchedulerTiming, TickHandler
from nalar.application.features.sessions.queries.session_state import SessionStateQuery
from nalar.application.features.sessions.timing import TurnTiming
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.factories import (
    World,
    build_world,
    create_mission_version,
    create_run,
    publish,
)
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import FakeClock, RecordingBackground

TIMING = SchedulerTiming(
    recovery_after=timedelta(seconds=15),
    evaluation_sweep_after=timedelta(minutes=2),
    finalize_cooldown=timedelta(minutes=10),
)
SESSION_ROW = "select status::text, ended_at, deadline_at from sessions where id = $1"


def tick(
    uow: PgUnitOfWork, clock: FakeClock, background: RecordingBackground | None = None
) -> TickHandler:
    return TickHandler(uow, clock, background or RecordingBackground(), TIMING)


async def queued(conn: DbConnection, session_id: UUID) -> int:
    count: int = await conn.fetchval(
        "select count(*) from pgmq.q_nalar_eval where message->>'session_id' = $1",
        str(session_id),
    )
    return count


async def test_window_run_opens_and_closes_on_time(conn: asyncpg.Connection, world: World) -> None:
    _, version_id = await create_mission_version(
        conn, world.school_id, world.kb_id, world.teacher_id, context_pack=world.pack
    )
    publication_id = await publish(
        conn, world.school_id, version_id, world.class_id, world.teacher_id
    )
    run_id = await create_run(conn, world.school_id, publication_id, mode="window")
    clock = FakeClock()
    await conn.execute(
        "update publication_runs set opens_at = $2, closes_at = $3 where id = $1",
        run_id,
        clock.now() + timedelta(minutes=5),
        clock.now() + timedelta(minutes=65),
    )
    status = "select status::text from publication_runs where id = $1"
    await tick(uow_on(conn), clock).execute()
    assert await conn.fetchval(status, run_id) == "scheduled"
    clock.advance(minutes=5)
    await tick(uow_on(conn), clock).execute()
    assert await conn.fetchval(status, run_id) == "open"
    clock.advance(minutes=60)
    await tick(uow_on(conn), clock).execute()
    assert await conn.fetchval(status, run_id) == "closed"


async def test_timeout_is_applied_once_by_read_or_scheduler(
    pool: asyncpg.Pool, eval_queue_guard: None
) -> None:
    async with pool.acquire() as c:
        world = await build_world(c, f"SMP Paralel {uuid4().hex[:6]}")
        await c.execute(
            "update sessions set started_at = now() - interval '30 minutes',"
            " deadline_at = now() - interval '10 minutes' where id = $1",
            world.session_id,
        )
    clock = FakeClock()
    read = SessionStateQuery(
        PgUnitOfWork(pool.acquire), clock, RecordingBackground(), TurnTiming(timedelta(seconds=15))
    )
    await asyncio.gather(
        tick(PgUnitOfWork(pool.acquire), clock).execute(),
        read.execute(world.student_id, world.session_id),
    )
    async with pool.acquire() as c:
        row = await c.fetchrow(SESSION_ROW, world.session_id)
        assert row is not None
        assert (row["status"], row["ended_at"]) == ("timed_out", row["deadline_at"])
        assert await queued(c, world.session_id) == 1


async def test_recovery_sweep_reruns_a_stuck_turn_once(
    conn: asyncpg.Connection, world: World
) -> None:
    clock, background = FakeClock(), RecordingBackground()
    await conn.execute(
        "update session_turns set answer_text = 'Jawaban', answer_submitted_at = $2"
        " where session_id = $1 and turn_index = 0",
        world.session_id,
        clock.now() - timedelta(seconds=30),
    )
    await tick(uow_on(conn), clock, background).execute()
    await conn.execute(
        "insert into session_turns"
        " (school_id, session_id, turn_index, prompt_kind, prompt_strategy, prompt_text)"
        " values ($1, $2, 1, 'probe', 'request_justification', 'Kenapa?')",
        world.school_id,
        world.session_id,
    )
    await conn.execute("update sessions set current_turn_index = 1 where id = $1", world.session_id)
    await tick(uow_on(conn), clock, background).execute()
    assert [s for s in background.turn_steps if s[0] == world.session_id] == [(world.session_id, 0)]


async def test_evaluation_sweep_skips_evaluated_and_queued_sessions(
    conn: asyncpg.Connection, world: World
) -> None:
    other = await build_world(conn, "SMP Sweep B")
    bare = await build_world(conn, "SMP Sweep C")
    clock = FakeClock()
    oldest = await conn.fetchval("select min(ended_at) from sessions")
    ended_at = min(oldest or clock.now(), clock.now()) - timedelta(minutes=5)
    for w in (world, other, bare):
        await conn.execute(
            "update sessions set status = 'completed', end_reason = 'student_completed',"
            " ended_at = $2 where id = $1",
            w.session_id,
            ended_at,
        )
    await conn.execute(
        "insert into session_evaluations (school_id, session_id) values ($1, $2)",
        world.school_id,
        world.session_id,
    )
    await conn.execute(
        "select pgmq.send('nalar_eval',"
        " jsonb_build_object('kind', 'evaluate_session', 'session_id', $1::text))",
        str(other.session_id),
    )
    await tick(uow_on(conn), clock).execute()
    assert [await queued(conn, w.session_id) for w in (world, other, bare)] == [0, 1, 1]


async def test_a_second_tick_changes_nothing(conn: asyncpg.Connection, world: World) -> None:
    clock = FakeClock()
    clock.advance(minutes=30)
    await tick(uow_on(conn), clock).execute()
    snapshot = await conn.fetchrow(SESSION_ROW, world.session_id)
    count = await queued(conn, world.session_id)
    await tick(uow_on(conn), clock).execute()
    assert await conn.fetchrow(SESSION_ROW, world.session_id) == snapshot
    assert await queued(conn, world.session_id) == count == 1
