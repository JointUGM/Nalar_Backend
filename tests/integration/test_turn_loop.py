import asyncio
from uuid import uuid4

import asyncpg

from nalar.application.features.sessions.commands.run_turn_step import RunTurnStepHandler
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.contract.forbidden import forbidden_keys, json_keys
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import FakeClock, RecordingBackground, ScriptedAiGateway
from tests.unit.application.test_run_turn_step import reply


async def answer_anchor(conn: DbConnection, world: World) -> None:
    await conn.execute(
        "update session_turns set answer_text = 'Karena gayanya habis',"
        " answer_submitted_at = now() where session_id = $1 and turn_index = 0",
        world.session_id,
    )


async def test_turn_step_twice_writes_one_next_turn(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as c:
        world = await build_world(c, f"SMP Paralel {uuid4().hex[:6]}")
        await answer_anchor(c, world)
    ai = ScriptedAiGateway()
    ai.script("next_turn", reply(pack=world.pack), reply(pack=world.pack))

    def handler() -> RunTurnStepHandler:
        return RunTurnStepHandler(PgUnitOfWork(pool.acquire), FakeClock(), ai)

    await asyncio.gather(
        handler().execute(world.session_id, 0), handler().execute(world.session_id, 0)
    )
    async with pool.acquire() as c:
        turns = await c.fetchval(
            "select count(*) from session_turns where session_id = $1", world.session_id
        )
        index = await c.fetchval(
            "select current_turn_index from sessions where id = $1", world.session_id
        )
    assert (turns, index) == (2, 1)


async def test_safety_pause_alerts_each_assigned_teacher_once(
    conn: asyncpg.Connection, world: World
) -> None:
    await answer_anchor(conn, world)
    ai = ScriptedAiGateway()
    ai.script("next_turn", reply("safety_pause", "safety", pack=world.pack))
    await RunTurnStepHandler(uow_on(conn), FakeClock(), ai).execute(world.session_id, 0)
    status = await conn.fetchval(
        "select status::text from sessions where id = $1", world.session_id
    )
    alerts = await conn.fetch(
        "select recipient_id, payload from notifications where type = 'wellbeing_alert'"
        " and payload->>'session_id' = $1",
        str(world.session_id),
    )
    assert status == "paused_safety"
    assert [a["recipient_id"] for a in alerts] == [world.teacher_id]


async def test_a_whole_session_runs_against_the_scripted_ai(
    conn: asyncpg.Connection, world: World
) -> None:
    clock, ai, background = FakeClock(), ScriptedAiGateway(), RecordingBackground()
    ai.script(
        "next_turn",
        reply(pack=world.pack),
        reply(pack=world.pack),
        reply("end", end_reason="coverage_complete", pack=world.pack),
    )
    step = RunTurnStepHandler(uow_on(conn), clock, ai)
    headers = as_user(world.student_id)
    url = f"/student/sessions/{world.session_id}"
    async with api_client(conn, clock=clock, ai=ai, background=background) as api:
        for turn_index in range(3):
            response = await api.post(
                f"{url}/answers",
                json={
                    "turn_index": turn_index,
                    "answer_text": f"Jawaban {turn_index}",
                    "client_submission_id": str(uuid4()),
                },
                headers=headers,
            )
            assert response.status_code == 202
            state = (await api.get(f"{url}/state", headers=headers)).json()
            assert not forbidden_keys(json_keys([response.json(), state]))
            assert state["status"] == "processing"
            await step.execute(world.session_id, turn_index)
        final = (await api.get(f"{url}/state", headers=headers)).json()
    assert not forbidden_keys(json_keys(final))
    queued = await conn.fetchval(
        "select count(*) from pgmq.q_nalar_eval where message->>'session_id' = $1",
        str(world.session_id),
    )
    states = await conn.fetch(
        "select answer_state::text from session_turns where session_id = $1 order by turn_index",
        world.session_id,
    )
    invocations = await conn.fetchval(
        "select count(*) from ai_invocations where school_id = $1", world.school_id
    )
    assert final["status"] == "evaluating"
    assert final["prompt"] is None
    assert queued == 1
    assert [s["answer_state"] for s in states] == ["misconception"] * 3
    assert invocations == 6
    assert background.turn_steps == [(world.session_id, i) for i in range(3)]
