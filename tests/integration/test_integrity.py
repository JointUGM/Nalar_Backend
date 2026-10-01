import json
from uuid import UUID, uuid4

import asyncpg

from nalar.application.features.evaluation.commands.evaluate_session import EvaluateSessionHandler
from nalar.application.features.integrity.commands.compute_publication_similarity import (
    ComputePublicationSimilarityHandler,
)
from nalar.application.features.integrity.commands.compute_session_flags import (
    ComputeSessionFlagsHandler,
)
from nalar.application.ports.ai import AiServiceError
from nalar.infrastructure.config import load_integrity_config
from tests.integration.support.factories import (
    World,
    create_session,
    create_student,
    evaluate,
    finish_session,
)
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import ScriptedAiGateway

AT = "2026-10-08T02:00:00Z"
COPIED = "Kelereng berhenti karena gaya gesek antara kelereng dan lantai yang kasar"


async def paste_on_anchor(conn: asyncpg.Connection, world: World, session_id: UUID) -> None:
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
