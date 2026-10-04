import json
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import RUBRIC, World, build_world, create_student
from tests.unit.application.fakes import FakeClock

OUTCOMES = ["mastered"] * 12 + ["developing"] * 8 + ["misconception"] * 7 + ["not_observed"] * 2


async def test_report_uses_published_rubric_and_deduplicated_activity(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "insert into mission_versions (school_id, mission_id, version_number, anchor_problem,"
        " rubric, created_by, probe_plan)"
        " values ($1, $2, 2, 'New draft', $3::jsonb, $4, '{}'::jsonb)",
        world.school_id,
        world.mission_id,
        json.dumps({dimension: ["Changed"] * 5 for dimension in RUBRIC}),
        world.teacher_id,
    )
    turn = await conn.fetchval(
        "select id from session_turns where session_id = $1", world.session_id
    )
    await conn.execute(
        "insert into turn_metrics (turn_id, school_id, chars_pasted) values ($1, $2, 9999)",
        turn,
        world.school_id,
    )
    await conn.execute(
        "insert into session_turns (school_id, session_id, turn_index, prompt_kind,"
        " prompt_text, prompt_strategy)"
        " values ($1, $2, 1, 'probe', 'Mengapa?', 'request_justification')",
        world.school_id,
        world.session_id,
    )
    body = {
        "client_seq": 1,
        "turn_index": 0,
        "events": [
            {"type": "paste", "at": "2026-10-03T00:00:00Z", "value": 80},
            {"type": "visibility_hidden", "at": "2026-10-03T00:00:00Z", "value": 1250},
            {
                "type": "typing",
                "at": "2026-10-03T00:00:00Z",
                "value": {"chars": 4, "duration_ms": 900},
            },
        ],
    }
    async with api_client(conn) as api:
        for _ in range(2):
            assert (
                await api.post(
                    f"/student/sessions/{world.session_id}/telemetry",
                    json=body,
                    headers=as_user(world.student_id),
                )
            ).status_code == 200
        unattributed = body | {"client_seq": 2, "turn_index": None}
        assert (
            await api.post(
                f"/student/sessions/{world.session_id}/telemetry",
                json=unattributed,
                headers=as_user(world.student_id),
            )
        ).status_code == 200
        response = await api.get(
            f"/sessions/{world.session_id}/report", headers=as_user(world.teacher_id)
        )
        assert response.status_code == 200, response.text
        student = await api.get(
            f"/sessions/{world.session_id}/report", headers=as_user(world.student_id)
        )
        assert student.status_code == 404
    report = response.json()
    assert report["mission"] == {
        "mission_id": str(world.mission_id),
        "title": "Gaya dan Gerak",
        "version_number": 1,
    }
    assert report["rubric"] == RUBRIC
    assert report["turns"][0]["activity"] == {
        "paste_chars": 80,
        "away_seconds": 1.25,
        "typing_ms": 900,
    }
    assert report["turns"][1]["activity"] == {"paste_chars": 0, "away_seconds": 0, "typing_ms": 0}


async def evaluated_session(
    conn: asyncpg.Connection,
    world: World,
    student_id: UUID,
    attempt: int,
    status: str,
    outcome: str | None,
) -> UUID:
    session_id: UUID = await conn.fetchval(
        "insert into sessions (school_id, publication_id, run_id, student_id, attempt_number,"
        " status, end_reason, started_at, ended_at, deadline_at)"
        " values ($1, $2, $3, $4, $5, $6::session_status,"
        " case when $6 = 'completed' then 'student_completed'::session_end_reason"
        " else 'max_duration_reached' end,"
        " now() - interval '30 minutes', now() - interval '10 minutes',"
        " now() - interval '10 minutes') returning id",
        world.school_id,
        world.publication_id,
        world.run_id,
        student_id,
        attempt,
        status,
    )
    if outcome is not None:
        misconception_id = await conn.fetchval(
            "select id from misconceptions where concept_id = $1", world.concept_ids[0]
        )
        await conn.execute(
            "insert into session_evaluations (school_id, session_id) values ($1, $2)",
            world.school_id,
            session_id,
        )
        await conn.execute(
            "insert into session_concept_results"
            " (school_id, session_id, concept_id, outcome, misconception_id)"
            " values ($1, $2, $3, $4::concept_outcome, $5)",
            world.school_id,
            session_id,
            world.concept_ids[0],
            outcome,
            misconception_id if outcome == "misconception" else None,
        )
    return session_id


async def test_class_map_counts_match_sql_on_32_students(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute("delete from session_turns where session_id = $1", world.session_id)
    await conn.execute("delete from sessions where id = $1", world.session_id)
    holders: dict[str, str] = {}
    for outcome in OUTCOMES:
        student = await create_student(conn, world.school_id, world.class_id, world.year_id)
        session_id = await evaluated_session(conn, world, student, 1, "completed", outcome)
        if outcome == "misconception" and not holders:
            session_id = await evaluated_session(conn, world, student, 2, "completed", outcome)
        if outcome == "misconception":
            holders[str(student)] = str(session_id)
    retried = await create_student(conn, world.school_id, world.class_id, world.year_id)
    await evaluated_session(conn, world, retried, 1, "completed", "mastered")
    await evaluated_session(conn, world, retried, 2, "timed_out", None)
    for _ in range(2):
        student = await create_student(conn, world.school_id, world.class_id, world.year_id)
        await evaluated_session(conn, world, student, 1, "timed_out", None)
    async with api_client(conn) as api:
        response = await api.get(
            f"/publications/{world.publication_id}/class-map", headers=as_user(world.teacher_id)
        )
    body = response.json()
    expected = await conn.fetch(
        "with latest as (select distinct on (student_id) id, status from sessions"
        " where publication_id = $1 order by student_id, attempt_number desc)"
        " select r.outcome::text, count(*) from latest l"
        " join session_concept_results r on r.session_id = l.id"
        " where l.status = 'completed' and r.concept_id = $2 group by r.outcome",
        world.publication_id,
        world.concept_ids[0],
    )
    counts = {r["outcome"]: r["count"] for r in expected}
    concept = next(c for c in body["concepts"] if c["concept_id"] == str(world.concept_ids[0]))
    assert (body["denominator"], body["incomplete_count"]) == (29, 3)
    assert concept["mastered_count"] == counts["mastered"] == 12
    assert concept["developing_count"] == counts["developing"]
    assert concept["misconceptions"][0]["count"] == counts["misconception"] == 7
    named = concept["misconceptions"][0]["students"]
    assert {i["student_id"]: i["session_id"] for i in named} == holders
    assert {i["student_id"] for i in named} == set(concept["misconceptions"][0]["student_ids"])
    assert all(i["name"] == "Siswa Uji" for i in named)
    assert body["insight"] is None


async def test_monitor_lists_every_enrolled_student(conn: asyncpg.Connection, world: World) -> None:
    await create_student(conn, world.school_id, world.class_id, world.year_id)
    async with api_client(conn) as api:
        response = await api.get(
            f"/publications/{world.publication_id}/monitor", headers=as_user(world.teacher_id)
        )
    body = response.json()
    assert sorted(s["status"] for s in body["students"]) == ["in_progress", "not_joined"]
    unjoined = next(s for s in body["students"] if s["status"] == "not_joined")
    assert unjoined["participant_id"] is None
    assert body["run"]["id"] == str(world.run_id)


async def test_report_carries_the_trace_scores_and_quotes(
    conn: asyncpg.Connection, world: World
) -> None:
    turn_id = await conn.fetchval(
        "update session_turns set answer_text = 'Karena gaya gesek'"
        " where session_id = $1 and turn_index = 0 returning id",
        world.session_id,
    )
    evaluation_id = await conn.fetchval(
        "insert into session_evaluations (school_id, session_id, summary)"
        " values ($1, $2, 'Ringkasan') returning id",
        world.school_id,
        world.session_id,
    )
    score_id = await conn.fetchval(
        "insert into evaluation_scores (school_id, evaluation_id, dimension, ai_level, final_level)"
        " values ($1, $2, 'claim', 2, 2) returning id",
        world.school_id,
        evaluation_id,
    )
    await conn.execute(
        "insert into score_evidence (school_id, score_id, turn_id, quote)"
        " values ($1, $2, $3, 'gaya gesek')",
        world.school_id,
        score_id,
        turn_id,
    )
    async with api_client(conn) as api:
        response = await api.get(
            f"/sessions/{world.session_id}/report", headers=as_user(world.teacher_id)
        )
    body = response.json()
    assert body["turns"][0]["answer"] == "Karena gaya gesek"
    assert body["scores"][0]["evidence"] == [{"turn_id": str(turn_id), "quote": "gaya gesek"}]
    assert body["evaluation"] == {"status": "completed", "summary": "Ringkasan"}


async def pause(conn: asyncpg.Connection, world: World) -> None:
    await conn.execute(
        "update session_turns set answer_text = 'Aku sedih', safety_paused = true"
        " where session_id = $1 and turn_index = 0",
        world.session_id,
    )
    await conn.execute(
        "update sessions set status = 'paused_safety' where id = $1", world.session_id
    )


def safety_url(world: World) -> str:
    return f"/sessions/{world.session_id}/safety-actions"


async def test_safety_resume_appends_a_fixed_question(
    conn: asyncpg.Connection, world: World
) -> None:
    await pause(conn, world)
    async with api_client(conn) as api:
        response = await api.post(
            safety_url(world), json={"action": "resume"}, headers=as_user(world.teacher_id)
        )
    turn = await conn.fetchrow(
        "select move_source::text, prompt_strategy::text from session_turns"
        " where session_id = $1 and turn_index = 1",
        world.session_id,
    )
    audits = await conn.fetchval(
        "select count(*) from audit_logs where entity_id = $1", world.session_id
    )
    assert response.json()["status"] == "in_progress"
    assert turn is not None
    assert (turn["move_source"], turn["prompt_strategy"]) == ("fixed_rule", "request_justification")
    assert audits == 1


async def test_safety_resume_after_the_deadline_is_409(
    conn: asyncpg.Connection, world: World
) -> None:
    await pause(conn, world)
    clock = FakeClock()
    clock.advance(minutes=25)
    async with api_client(conn, clock=clock) as api:
        response = await api.post(
            safety_url(world), json={"action": "resume"}, headers=as_user(world.teacher_id)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "SESSION_DEADLINE_PASSED",
    )


async def test_safety_resume_at_the_last_turn_returns_completed(
    conn: asyncpg.Connection, world: World
) -> None:
    await pause(conn, world)
    await conn.execute(
        "update session_turns set turn_index = mv.max_turns"
        " from publications p join mission_versions mv on mv.id = p.mission_version_id"
        " where session_id = $1 and p.id = $2",
        world.session_id,
        world.publication_id,
    )
    async with api_client(conn) as api:
        response = await api.post(
            safety_url(world), json={"action": "resume"}, headers=as_user(world.teacher_id)
        )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert (
        await conn.fetchval("select status::text from sessions where id = $1", world.session_id)
        == "completed"
    )
    assert (
        await conn.fetchval(
            "select count(*) from pgmq.q_nalar_eval where message->>'session_id' = $1",
            str(world.session_id),
        )
        == 1
    )


async def test_safety_end_ends_the_session_and_queues_one_evaluation(
    conn: asyncpg.Connection, world: World
) -> None:
    await pause(conn, world)
    async with api_client(conn) as api:
        response = await api.post(
            safety_url(world),
            json={"action": "end", "note": "Dibicarakan"},
            headers=as_user(world.teacher_id),
        )
        again = await api.post(
            safety_url(world), json={"action": "end"}, headers=as_user(world.teacher_id)
        )
    row = await conn.fetchrow(
        "select status::text, end_reason::text from sessions where id = $1", world.session_id
    )
    queued = await conn.fetchval(
        "select count(*) from pgmq.q_nalar_eval where message->>'session_id' = $1",
        str(world.session_id),
    )
    assert response.json()["status"] == "ended_safety"
    assert row is not None
    assert (row["status"], row["end_reason"]) == ("ended_safety", "safety_pause")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "SESSION_NOT_PAUSED")
    assert queued == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/publications/{p}/monitor"),
        ("get", "/publications/{p}/class-map"),
        ("get", "/sessions/{s}/report"),
        ("post", "/sessions/{s}/safety-actions"),
    ],
)
async def test_another_teacher_gets_404(
    conn: asyncpg.Connection, world: World, method: str, path: str
) -> None:
    other = await build_world(conn, "SMP Lain")
    url = path.format(p=world.publication_id, s=world.session_id)
    kwargs: dict[str, Any] = {"json": {"action": "end"}} if method == "post" else {}
    async with api_client(conn) as api:
        response = await getattr(api, method)(url, headers=as_user(other.teacher_id), **kwargs)
    assert response.status_code == 404
