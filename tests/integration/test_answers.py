import asyncio
import time
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from nalar.application.errors import Conflict
from nalar.application.features.sessions.commands.submit_answer import (
    Accepted,
    SubmitAnswer,
    SubmitAnswerHandler,
)
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.contract.forbidden import forbidden_keys, json_keys
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world
from tests.unit.application.fakes import FakeClock, RecordingBackground


def answer(text: str, submission: UUID | None = None, turn_index: int = 0) -> dict[str, object]:
    return {
        "turn_index": turn_index,
        "answer_text": text,
        "client_submission_id": str(submission or uuid4()),
    }


def state_url(world: World) -> str:
    return f"/student/sessions/{world.session_id}/state"


def answers_url(world: World) -> str:
    return f"/student/sessions/{world.session_id}/answers"


async def test_answer_is_idempotent_and_never_creates_two_next_turns(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as c:
        world = await build_world(c, f"SMP Paralel {uuid4().hex[:6]}")
        racing = await build_world(c, f"SMP Paralel {uuid4().hex[:6]}")
    background = RecordingBackground()

    def handler() -> SubmitAnswerHandler:
        return SubmitAnswerHandler(PgUnitOfWork(pool.acquire), FakeClock(), background)

    same = SubmitAnswer(world.student_id, world.session_id, 0, "Karena gaya gesek", uuid4())
    assert await handler().execute(same) == await handler().execute(same)
    with pytest.raises(Conflict) as caught:
        await handler().execute(
            SubmitAnswer(world.student_id, world.session_id, 0, "Jawaban lain", uuid4())
        )
    assert caught.value.code == "TURN_ALREADY_ANSWERED"

    results = await asyncio.gather(
        handler().execute(
            SubmitAnswer(racing.student_id, racing.session_id, 0, "Jawaban A", uuid4())
        ),
        handler().execute(
            SubmitAnswer(racing.student_id, racing.session_id, 0, "Jawaban B", uuid4())
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Accepted) for r in results) == 1
    assert [r.code for r in results if isinstance(r, Conflict)] == ["TURN_ALREADY_ANSWERED"]
    assert background.turn_steps == [(world.session_id, 0), (racing.session_id, 0)]


async def test_answer_to_a_turn_that_is_not_open_is_409_turn_not_current(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        response = await api.post(
            answers_url(world),
            json=answer("Jawaban", turn_index=1),
            headers=as_user(world.student_id),
        )
    assert (response.status_code, response.json()["error"]["code"]) == (409, "TURN_NOT_CURRENT")


async def test_answer_after_the_deadline_is_409_session_deadline_passed(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    clock.advance(minutes=21)
    async with api_client(conn, clock=clock) as api:
        response = await api.post(
            answers_url(world), json=answer("Terlambat"), headers=as_user(world.student_id)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "SESSION_DEADLINE_PASSED",
    )


async def test_another_students_session_is_404(conn: asyncpg.Connection, world: World) -> None:
    other = await build_world(conn, "SMP Lain")
    async with api_client(conn) as api:
        post = await api.post(
            answers_url(world), json=answer("x"), headers=as_user(other.student_id)
        )
        get = await api.get(state_url(world), headers=as_user(other.student_id))
    assert (post.status_code, get.status_code) == (404, 404)


async def test_state_shows_the_open_prompt_and_nothing_hidden(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        body = (await api.get(state_url(world), headers=as_user(world.student_id))).json()
    assert not forbidden_keys(json_keys(body))
    assert body["status"] == "awaiting_answer"
    assert body["prompt"] == {
        "kind": "anchor",
        "text": "Kenapa kelereng berhenti?",
        "turn_index": 0,
    }
    assert set(body) == {
        "status",
        "turn_index",
        "probe_number",
        "probe_total",
        "started_at",
        "deadline_at",
        "prompt",
        "safety_message",
        "reflection_ready",
        "server_now",
    }


async def test_state_times_out_an_overdue_session_once(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    clock.advance(minutes=25)
    async with api_client(conn, clock=clock) as api:
        first = (await api.get(state_url(world), headers=as_user(world.student_id))).json()
        await api.get(state_url(world), headers=as_user(world.student_id))
    row = await conn.fetchrow(
        "select status::text, ended_at, deadline_at from sessions where id = $1", world.session_id
    )
    queued = await conn.fetchval(
        "select count(*) from pgmq.q_nalar_eval where message->>'session_id' = $1",
        str(world.session_id),
    )
    assert row is not None
    assert first["status"] == "evaluating"
    assert (row["status"], row["ended_at"], queued) == ("timed_out", row["deadline_at"], 1)


async def test_state_rekicks_a_turn_stuck_in_processing(
    conn: asyncpg.Connection, world: World
) -> None:
    clock, background = FakeClock(), RecordingBackground()
    await conn.execute(
        "update session_turns set answer_text = 'Jawaban', answer_submitted_at = $2"
        " where session_id = $1 and turn_index = 0",
        world.session_id,
        clock.now() - timedelta(seconds=20),
    )
    async with api_client(conn, clock=clock, background=background) as api:
        body = (await api.get(state_url(world), headers=as_user(world.student_id))).json()
    assert body["status"] == "processing"
    assert background.turn_steps == [(world.session_id, 0)]


async def test_answer_is_acknowledged_within_300_ms(conn: asyncpg.Connection, world: World) -> None:
    async with api_client(conn) as api:
        await api.get(state_url(world), headers=as_user(world.student_id))
        started = time.perf_counter()
        response = await api.post(
            answers_url(world), json=answer("Karena gesekan"), headers=as_user(world.student_id)
        )
        elapsed = time.perf_counter() - started
    assert response.status_code == 202
    assert response.json() == {
        "status": "processing",
        "next_prompt_url": f"/api/v1/student/sessions/{world.session_id}/state",
    }
    assert elapsed < 0.3
