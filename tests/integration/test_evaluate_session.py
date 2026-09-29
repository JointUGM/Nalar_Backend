from typing import Any
from uuid import UUID

import asyncpg
import pytest

from nalar.application.features.evaluation.commands.evaluate_session import EvaluateSessionHandler
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import EvaluateOut
from tests.contract.forbidden import forbidden_keys, json_keys
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import ScriptedAiGateway, invocation

ANSWER = "Kelereng berhenti karena gaya gesek dengan lantai."


async def finish(conn: asyncpg.Connection, world: World) -> UUID:
    turn_id: UUID = await conn.fetchval(
        "update session_turns set answer_text = $2, answer_submitted_at = now()"
        " where session_id = $1 and turn_index = 0 returning id",
        world.session_id,
        ANSWER,
    )
    await conn.execute(
        "update sessions set status = 'completed', end_reason = 'student_completed',"
        " ended_at = now() where id = $1",
        world.session_id,
    )
    return turn_id


def evaluation(
    world: World, turn_id: UUID, quote: str = "karena gaya gesek"
) -> AiResult[EvaluateOut]:
    body: dict[str, Any] = {
        "summary": "Siswa menjelaskan gaya gesek.",
        "turn_quality": [{"turn_id": str(turn_id), "turn_index": 0, "quality": 3}],
        "scores": [
            {
                "dimension": d,
                "level": 2,
                "rationale": "Cukup.",
                "evidence": [{"turn_id": str(turn_id), "quote": quote}],
            }
            for d in ("claim", "evidence", "mechanism", "transfer")
        ],
        "concept_results": [
            {
                "concept_id": str(world.concept_ids[0]),
                "outcome": "mastered",
                "misconception_id": None,
                "initial_misconception_id": None,
                "resolved_in_session": False,
                "evidence_turn_id": str(turn_id),
            },
            {
                "concept_id": str(world.concept_ids[1]),
                "outcome": "misconception",
                "misconception_id": next(
                    m["id"]
                    for m in world.pack["misconceptions"]
                    if m["concept_id"] == str(world.concept_ids[1])
                ),
                "initial_misconception_id": None,
                "resolved_in_session": False,
                "evidence_turn_id": str(turn_id),
            },
        ],
        "reflection": {
            "content": "Kamu sudah memikirkan apa yang membuat kelereng berhenti.",
            "source": "model",
        },
    }
    return AiResult(
        EvaluateOut.model_validate(body),
        [invocation("session_evaluation"), invocation("reflection_generation")],
    )


async def evaluation_row(conn: asyncpg.Connection, world: World) -> asyncpg.Record | None:
    row: asyncpg.Record | None = await conn.fetchrow(
        "select status::text, summary from session_evaluations where session_id = $1",
        world.session_id,
    )
    return row


async def invocations(conn: asyncpg.Connection, world: World) -> int:
    count: int = await conn.fetchval(
        "select count(*) from ai_invocations where school_id = $1", world.school_id
    )
    return count


async def test_a_completed_evaluation_writes_every_table(
    conn: asyncpg.Connection, world: World
) -> None:
    turn_id = await finish(conn, world)
    ai = ScriptedAiGateway()
    ai.script("evaluate_session", evaluation(world, turn_id))
    await EvaluateSessionHandler(uow_on(conn), ai).execute(world.session_id)
    row = await evaluation_row(conn, world)
    assert row is not None and row["status"] == "completed"
    levels = await conn.fetch(
        "select s.ai_level, s.final_level,"
        " (select count(*) from score_evidence e where e.score_id = s.id) as quotes"
        " from evaluation_scores s join session_evaluations v on v.id = s.evaluation_id"
        " where v.session_id = $1",
        world.session_id,
    )
    assert len(levels) == 4
    assert all(r["ai_level"] == r["final_level"] == 2 and r["quotes"] == 1 for r in levels)
    results = await conn.fetchval(
        "select count(*) from session_concept_results where session_id = $1", world.session_id
    )
    assert results == 2
    assert await conn.fetchval(
        "select content from session_reflections where session_id = $1", world.session_id
    )
    assert await invocations(conn, world) == 2


async def test_a_fabricated_quote_is_retried_once_then_failed(
    conn: asyncpg.Connection, world: World
) -> None:
    turn_id = await finish(conn, world)
    ai = ScriptedAiGateway()
    ai.script(
        "evaluate_session",
        evaluation(world, turn_id, "gayanya habis"),
        evaluation(world, turn_id, "gayanya habis"),
    )
    await EvaluateSessionHandler(uow_on(conn), ai).execute(world.session_id)
    row = await evaluation_row(conn, world)
    assert row is not None and row["status"] == "failed"
    assert len(ai.calls) == 2
    assert await invocations(conn, world) == 4


async def test_422_is_no_answer(conn: asyncpg.Connection, world: World) -> None:
    await finish(conn, world)
    ai = ScriptedAiGateway()
    ai.script(
        "evaluate_session",
        AiServiceError("no_answer", 422, [invocation("session_evaluation", "error")]),
    )
    await EvaluateSessionHandler(uow_on(conn), ai).execute(world.session_id)
    row = await evaluation_row(conn, world)
    assert row is not None and row["status"] == "no_answer"
    assert await invocations(conn, world) == 1


async def test_502_twice_is_failed(conn: asyncpg.Connection, world: World) -> None:
    await finish(conn, world)
    ai = ScriptedAiGateway()
    failure = AiServiceError("ai_output_invalid", 502, [invocation("session_evaluation", "error")])
    ai.script("evaluate_session", failure, failure)
    await EvaluateSessionHandler(uow_on(conn), ai).execute(world.session_id)
    row = await evaluation_row(conn, world)
    assert row is not None and row["status"] == "failed"
    assert await invocations(conn, world) == 2


async def test_503_leaves_the_message_for_a_retry(conn: asyncpg.Connection, world: World) -> None:
    await finish(conn, world)
    ai = ScriptedAiGateway()
    ai.script(
        "evaluate_session",
        AiServiceError("upstream_unavailable", 503, [invocation("session_evaluation", "error")]),
    )
    with pytest.raises(AiServiceError):
        await EvaluateSessionHandler(uow_on(conn), ai).execute(world.session_id)
    assert await evaluation_row(conn, world) is None
    assert await invocations(conn, world) == 1


async def test_an_existing_evaluation_skips_the_ai(conn: asyncpg.Connection, world: World) -> None:
    await finish(conn, world)
    await conn.execute(
        "insert into session_evaluations (school_id, session_id) values ($1, $2)",
        world.school_id,
        world.session_id,
    )
    ai = ScriptedAiGateway()
    await EvaluateSessionHandler(uow_on(conn), ai).execute(world.session_id)
    assert ai.calls == []


async def test_reflection_is_pending_then_stored_and_stable(
    conn: asyncpg.Connection, world: World
) -> None:
    turn_id = await finish(conn, world)
    url, headers = f"/student/sessions/{world.session_id}/reflection", as_user(world.student_id)
    async with api_client(conn) as api:
        assert (await api.get(url, headers=headers)).status_code == 202
        ai = ScriptedAiGateway()
        ai.script("evaluate_session", evaluation(world, turn_id))
        await EvaluateSessionHandler(uow_on(conn), ai).execute(world.session_id)
        first, second = await api.get(url, headers=headers), await api.get(url, headers=headers)
    assert first.status_code == 200
    assert first.json() == second.json()
    assert not forbidden_keys(json_keys(first.json()))
    assert set(first.json()) == {"mission_title", "completed_at", "content", "opening_guess"}


async def test_no_answer_has_no_reflection(conn: asyncpg.Connection, world: World) -> None:
    await finish(conn, world)
    await conn.execute(
        "insert into session_evaluations (school_id, session_id, status)"
        " values ($1, $2, 'no_answer')",
        world.school_id,
        world.session_id,
    )
    async with api_client(conn) as api:
        response = await api.get(
            f"/student/sessions/{world.session_id}/reflection", headers=as_user(world.student_id)
        )
    assert response.status_code == 404
