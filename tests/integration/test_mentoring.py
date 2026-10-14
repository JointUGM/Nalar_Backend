from uuid import UUID

import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, create_session, create_student


async def test_monitor_signal_revalidates_and_clears_after_recovery(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "update session_turns set answer_text = 'Sulit', answer_submitted_at = now(),"
        " frustration_signal = true where session_id = $1",
        world.session_id,
    )
    url = f"/publications/{world.publication_id}/monitor"
    async with api_client(conn) as api:
        first = await api.get(url, headers=as_user(world.teacher_id))
        assert first.status_code == 200, first.text
        signal = first.json()["students"][0]["help_signal"]
        assert signal["reason"] == "frustration" and signal["rank"] == 1
        headers = as_user(world.teacher_id) | {"If-None-Match": first.headers["etag"]}
        assert (await api.get(url, headers=headers)).status_code == 304
        assert (await api.get(url, headers=as_user(world.parent_id))).status_code == 404
        await conn.execute(
            "update session_turns set frustration_signal = false,"
            " answer_state = 'correct_reasoned' where session_id = $1",
            world.session_id,
        )
        recovered = await api.get(url, headers=headers)
        assert recovered.status_code == 200
        assert recovered.json()["students"][0]["help_signal"] is None


async def test_monitor_caps_signals_and_excludes_terminal_and_old_attempts(
    conn: asyncpg.Connection, world: World
) -> None:
    for _ in range(31):
        student = await create_student(conn, world.school_id, world.class_id, world.year_id)
        session = await create_session(
            conn,
            world.school_id,
            world.publication_id,
            world.run_id,
            student,
        )
        await conn.execute(
            "update session_turns set answer_text = 'Sulit',"
            " answer_submitted_at = now(), frustration_signal = true where session_id = $1",
            session,
        )
    await conn.execute(
        "update session_turns set answer_text = 'Sulit', frustration_signal = true"
        " where session_id = $1",
        world.session_id,
    )
    await create_session(
        conn,
        world.school_id,
        world.publication_id,
        world.run_id,
        world.student_id,
        2,
    )
    url = f"/publications/{world.publication_id}/monitor"
    async with api_client(conn) as api:
        first = await api.get(url, headers=as_user(world.teacher_id))
        assert first.status_code == 200, first.text
        students = first.json()["students"]
        signals = [s["help_signal"] for s in students if s["help_signal"]]
        assert sorted(s["rank"] for s in signals) == [1, 2, 3, 4, 5]
        assert (
            next(s for s in students if s["student_id"] == str(world.student_id))["help_signal"]
            is None
        )
        await conn.execute(
            "update sessions set status = 'completed', end_reason = 'student_completed',"
            " ended_at = now() where run_id = $1",
            world.run_id,
        )
        terminal = await api.get(url, headers=as_user(world.teacher_id))
        assert terminal.status_code == 200, terminal.text
        assert all(s["help_signal"] is None for s in terminal.json()["students"])


async def test_table_mode_counter_example_uses_the_answered_row_and_frozen_question(
    conn: asyncpg.Connection, world: World
) -> None:
    question = next(
        q
        for q in world.pack["question_bank"]
        if q["move"] == "counter_example" and q.get("misconception_id")
    )
    misconception_id = UUID(question["misconception_id"])
    await conn.execute(
        "update session_turns set answer_text = 'Tetap sama', answer_submitted_at = now(),"
        " answer_state = 'misconception', detected_misconception_id = $2"
        " where session_id = $1",
        world.session_id,
        misconception_id,
    )
    await conn.execute(
        "insert into session_turns (school_id, session_id, turn_index, prompt_kind,"
        " prompt_strategy, prompt_text, answer_text, answer_submitted_at, answer_state,"
        " detected_misconception_id, target_concept_id, question_bank_id)"
        " values ($1,$2,1,'probe','counter_example',$3,'Tetap sama',now(),"
        " 'misconception',$4,$5,$6)",
        world.school_id,
        world.session_id,
        question["text"],
        misconception_id,
        UUID(question["concept_id"]),
        question["id"],
    )
    async with api_client(conn) as api:
        response = await api.get(
            f"/publications/{world.publication_id}/monitor",
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 200, response.text
    signal = response.json()["students"][0]["help_signal"]
    assert signal["reason"] == "persistent_misconception"
    assert signal["turn_index"] == 1
    assert signal["suggestion"] == f"Coba tanyakan: {question['text']}"
