import asyncio
import json
from uuid import UUID, uuid4

import asyncpg
import pytest

from nalar.application.features.evaluation.commands.evaluate_session import EvaluateSessionHandler
from nalar.application.features.integrity.commands.compute_live_session_flags import (
    ComputeLiveSessionFlagsHandler,
)
from nalar.application.features.integrity.commands.compute_publication_similarity import (
    ComputePublicationSimilarityHandler,
)
from nalar.application.features.integrity.commands.compute_session_flags import (
    ComputeSessionFlagsHandler,
)
from nalar.application.ports.ai import AiServiceError
from nalar.domain.integrity import FlagDraft
from nalar.domain.telemetry import metrics_by_turn
from nalar.infrastructure.config import load_integrity_config
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.integrity import PgIntegrityRepo
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.factories import (
    World,
    build_world,
    create_session,
    create_student,
    evaluate,
    finish_session,
)
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import ScriptedAiGateway

AT = "2026-10-08T02:00:00Z"
COPIED = "Kelereng berhenti karena gaya gesek antara kelereng dan lantai yang kasar"


async def paste_on_anchor(conn: DbConnection, world: World, session_id: UUID) -> None:
    await conn.execute(
        "insert into telemetry_batches (school_id, session_id, turn_id, client_seq, events)"
        " select $1, $2, t.id, 1, $3::jsonb from session_turns t"
        " where t.session_id = $2 and t.turn_index = 0",
        world.school_id,
        session_id,
        json.dumps([{"type": "paste", "at": AT, "value": 70}]),
    )


async def test_session_flags_are_written_once_with_metrics(
    conn: asyncpg.Connection, world: World
) -> None:
    await finish_session(conn, world, world.session_id, answer=COPIED)
    await evaluate(conn, world, world.session_id)
    await paste_on_anchor(conn, world, world.session_id)
    handler = ComputeSessionFlagsHandler(uow_on(conn), load_integrity_config())
    assert await handler.execute(world.session_id) == 1
    assert await handler.execute(world.session_id) == 0
    flags = await conn.fetch(
        "select flag_type::text, evidence from authenticity_flags where session_id = $1",
        world.session_id,
    )
    assert [f["flag_type"] for f in flags] == ["large_paste"]
    assert json.loads(flags[0]["evidence"])["config_version"] == 1
    assert (
        await conn.fetchval(
            "select m.chars_pasted from turn_metrics m join session_turns t on t.id = m.turn_id"
            " where t.session_id = $1",
            world.session_id,
        )
        == 70
    )


async def test_evaluation_enqueues_session_flags_in_its_transaction(
    conn: asyncpg.Connection, world: World
) -> None:
    await finish_session(conn, world, world.session_id)
    before = await conn.fetchval("select coalesce(max(msg_id), 0) from pgmq.q_nalar_default")
    ai = ScriptedAiGateway()
    ai.script("evaluate_session", AiServiceError("no_answer", 422))
    await EvaluateSessionHandler(uow_on(conn), ai).execute(world.session_id)
    bodies = await conn.fetch(
        "select message from pgmq.q_nalar_default where msg_id > $1"
        " and message->>'kind' = 'compute_session_flags'",
        before,
    )
    assert [json.loads(b["message"]) for b in bodies] == [
        {"kind": "compute_session_flags", "session_id": str(world.session_id)}
    ]


async def test_similarity_flags_both_students_once(conn: asyncpg.Connection, world: World) -> None:
    other = await create_student(conn, world.school_id, world.class_id, world.year_id)
    second = await create_session(conn, world.school_id, world.publication_id, world.run_id, other)
    for session_id in (world.session_id, second):
        await finish_session(conn, world, session_id, answer=COPIED)
        await evaluate(conn, world, session_id)
    handler = ComputePublicationSimilarityHandler(uow_on(conn), load_integrity_config())
    assert await handler.execute(world.publication_id) == 2
    assert await handler.execute(world.publication_id) == 0
    assert (
        await conn.fetchval(
            "select count(*) from authenticity_flags where flag_type = 'cross_student_similarity'"
            " and session_id = any($1::uuid[])",
            [world.session_id, second],
        )
        == 2
    )


async def test_missing_session_is_a_no_op(conn: asyncpg.Connection) -> None:
    handler = ComputeSessionFlagsHandler(uow_on(conn), load_integrity_config())
    assert await handler.execute(uuid4()) == 0


@pytest.mark.parametrize("per_turn", [True, False])
async def test_racing_flag_inserts_preserve_one_identity_and_its_review(
    pool: asyncpg.Pool, per_turn: bool
) -> None:
    async with pool.acquire() as setup, setup.transaction():
        world = await build_world(setup)
        turn_id = await setup.fetchval(
            "select id from session_turns where session_id = $1 and turn_index = 0",
            world.session_id,
        )
    draft = FlagDraft(
        "large_paste" if per_turn else "tab_switching",
        "medium",
        turn_id if per_turn else None,
        {"config_version": 1},
    )
    task: asyncio.Task[int] | None = None
    try:
        async with pool.acquire() as first, pool.acquire() as second:
            attempting = asyncio.Event()

            async def racing_insert() -> int:
                async with second.transaction():
                    attempting.set()
                    return await PgIntegrityRepo(second).insert_flags(
                        world.school_id, world.session_id, [draft]
                    )

            async with first.transaction():
                assert (
                    await PgIntegrityRepo(first).insert_flags(
                        world.school_id, world.session_id, [draft]
                    )
                    == 1
                )
                task = asyncio.create_task(racing_insert())
                await attempting.wait()
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), 0.2)
            assert await asyncio.wait_for(task, 5) == 0
            assert (
                await first.fetchval(
                    "select count(*) from authenticity_flags where session_id = $1",
                    world.session_id,
                )
                == 1
            )
            await first.execute(
                "update authenticity_flags set status = 'cleared', reviewed_by = $2,"
                " reviewed_at = now(), review_note = 'Sudah ditinjau' where session_id = $1",
                world.session_id,
                world.teacher_id,
            )
            before = await first.fetchrow(
                "select * from authenticity_flags where session_id = $1", world.session_id
            )
            changed = FlagDraft(
                draft.flag_type, draft.severity, draft.turn_id, {"config_version": 2}
            )
            assert (
                await PgIntegrityRepo(first).insert_flags(
                    world.school_id, world.session_id, [changed]
                )
                == 0
            )
            assert (
                await first.fetchrow(
                    "select * from authenticity_flags where session_id = $1", world.session_id
                )
                == before
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with pool.acquire() as cleanup:
            await cleanup.execute("delete from sessions where id = $1", world.session_id)


async def test_computation_lock_keeps_later_metrics_from_an_older_snapshot(
    pool: asyncpg.Pool,
) -> None:
    async with pool.acquire() as setup, setup.transaction():
        world = await build_world(setup)
        await finish_session(setup, world, world.session_id, answer=COPIED)
        await evaluate(setup, world, world.session_id)
        await paste_on_anchor(setup, world, world.session_id)
    task: asyncio.Task[int] | None = None
    try:
        async with pool.acquire() as first, pool.acquire() as writer:
            async with first.transaction():
                repo = PgIntegrityRepo(first)
                assert await repo.lock_session(world.session_id)
                data = await repo.session_input(world.session_id)
                assert data is not None
                before = metrics_by_turn(data.batches)
                turn_id = data.turns[0][0]
                task = asyncio.create_task(
                    ComputeSessionFlagsHandler(
                        PgUnitOfWork(pool.acquire), load_integrity_config()
                    ).execute(world.session_id)
                )
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), 0.2)
                async with writer.transaction():
                    await writer.execute(
                        "insert into telemetry_batches"
                        " (school_id, session_id, turn_id, client_seq, events)"
                        " values ($1, $2, $3, 2, $4::jsonb)",
                        world.school_id,
                        world.session_id,
                        turn_id,
                        json.dumps([{"type": "paste", "at": AT, "value": 30}]),
                    )
                await repo.upsert_metrics(world.school_id, before)
            assert await asyncio.wait_for(task, 5) == 1
            assert (
                await first.fetchval(
                    "select chars_pasted from turn_metrics where turn_id = $1", turn_id
                )
                == 100
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with pool.acquire() as cleanup:
            await cleanup.execute("delete from sessions where id = $1", world.session_id)


async def test_live_paste_detection_invariants_across_arrival_order(
    conn: asyncpg.Connection, world: World
) -> None:
    handler = ComputeLiveSessionFlagsHandler(uow_on(conn), load_integrity_config())

    # Case A: telemetry before answer
    await paste_on_anchor(conn, world, world.session_id)
    assert await handler.execute(world.session_id) == 0
    await conn.execute(
        "update session_turns set answer_text = $1 where session_id = $2 and turn_index = 0",
        COPIED,
        world.session_id,
    )
    assert await handler.execute(world.session_id) == 1
    assert await handler.execute(world.session_id) == 0

    # Case B: answer before telemetry
    other_student = await create_student(conn, world.school_id, world.class_id, world.year_id)
    second_session = await create_session(
        conn, world.school_id, world.publication_id, world.run_id, other_student
    )
    await conn.execute(
        "update session_turns set answer_text = $1 where session_id = $2 and turn_index = 0",
        COPIED,
        second_session,
    )
    assert await handler.execute(second_session) == 0
    await paste_on_anchor(conn, world, second_session)
    assert await handler.execute(second_session) == 1
    assert await handler.execute(second_session) == 0

    flags = await conn.fetch(
        "select session_id, flag_type::text from authenticity_flags where session_id in ($1, $2)",
        world.session_id,
        second_session,
    )
    assert len(flags) == 2
    assert all(f["flag_type"] == "large_paste" for f in flags)


async def test_live_tab_switching_flag_unanswered(conn: asyncpg.Connection, world: World) -> None:
    await conn.execute(
        "insert into telemetry_batches (school_id, session_id, turn_id, client_seq, events)"
        " select $1, $2, t.id, 1, $3::jsonb from session_turns t"
        " where t.session_id = $2 and t.turn_index = 0",
        world.school_id,
        world.session_id,
        json.dumps([{"type": "visibility_hidden", "at": AT, "value": 20000}]),
    )
    handler = ComputeLiveSessionFlagsHandler(uow_on(conn), load_integrity_config())
    assert await handler.execute(world.session_id) == 1
    assert await handler.execute(world.session_id) == 0

    flag = await conn.fetchrow(
        "select flag_type::text from authenticity_flags where session_id = $1",
        world.session_id,
    )
    assert flag is not None and flag["flag_type"] == "tab_switching"


async def test_live_safety_paused_session_is_ignored(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "update sessions set status = 'paused_safety' where id = $1",
        world.session_id,
    )
    await paste_on_anchor(conn, world, world.session_id)
    handler = ComputeLiveSessionFlagsHandler(uow_on(conn), load_integrity_config())
    assert await handler.execute(world.session_id) == 0
    flags = await conn.fetch(
        "select id from authenticity_flags where session_id = $1", world.session_id
    )
    assert len(flags) == 0
