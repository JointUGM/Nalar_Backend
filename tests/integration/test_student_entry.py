from datetime import timedelta
from uuid import UUID

import asyncpg

from nalar.domain import join_code
from tests.contract.forbidden import forbidden_keys, json_keys
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    add_membership,
    build_world,
    create_mission_version,
    create_run,
    create_student,
    create_user,
    open_lobby_run,
    publish,
)
from tests.unit.application.fakes import FakeClock


async def lobby(conn: asyncpg.Connection, world: World) -> str:
    code = join_code.generate()
    await open_lobby_run(conn, world, code)
    return code


async def new_student(conn: asyncpg.Connection, world: World) -> UUID:
    return await create_student(conn, world.school_id, world.class_id, world.year_id)


async def test_lobby_join_creates_a_waiting_participant_and_a_rejoin_reuses_it(
    conn: asyncpg.Connection, world: World
) -> None:
    code, student = await lobby(conn, world), await new_student(conn, world)
    async with api_client(conn) as api:
        first = await api.post(
            "/student/runs/join", json={"join_code": code.lower()}, headers=as_user(student)
        )
        second = await api.post(
            "/student/runs/join", json={"join_code": code}, headers=as_user(student)
        )
    assert first.status_code == 200
    assert not forbidden_keys(json_keys([first.json(), second.json()]))
    assert first.json()["run_status"] == "lobby"
    assert first.json()["session_id"] is None
    assert [c["id"] for c in first.json()["warmup"]["choices"]] == ["a", "b", "c"]
    assert second.json()["participant_id"] == first.json()["participant_id"]


async def test_late_join_to_an_open_run_gets_its_own_started_at(
    conn: asyncpg.Connection, world: World
) -> None:
    code, student = await lobby(conn, world), await new_student(conn, world)
    await conn.execute(
        "update publication_runs set status = 'open', started_at = now() - interval '5 minutes'"
        " where id = $1",
        world.run_id,
    )
    clock = FakeClock()
    async with api_client(conn, clock=clock) as api:
        response = await api.post(
            "/student/runs/join", json={"join_code": code}, headers=as_user(student)
        )
    body = response.json()
    row = await conn.fetchrow(
        "select started_at, deadline_at from sessions where id = $1", UUID(body["session_id"])
    )
    assert body["run_status"] == "open"
    assert body["warmup"] is None
    assert row is not None
    assert (row["started_at"], row["deadline_at"]) == (
        clock.now(),
        clock.now() + timedelta(minutes=20),
    )


async def test_closed_run_code_is_409_run_not_joinable(
    conn: asyncpg.Connection, world: World
) -> None:
    code, student = await lobby(conn, world), await new_student(conn, world)
    await conn.execute(
        "update publication_runs set status = 'closed', closed_at = now() where id = $1",
        world.run_id,
    )
    async with api_client(conn) as api:
        response = await api.post(
            "/student/runs/join", json={"join_code": code}, headers=as_user(student)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (409, "RUN_NOT_JOINABLE")


async def test_unknown_code_is_404_join_code_invalid(
    conn: asyncpg.Connection, world: World
) -> None:
    student = await new_student(conn, world)
    async with api_client(conn) as api:
        response = await api.post(
            "/student/runs/join", json={"join_code": "ZZZZZZ"}, headers=as_user(student)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (404, "JOIN_CODE_INVALID")


async def test_same_school_student_not_in_the_class_is_403_not_enrolled(
    conn: asyncpg.Connection, world: World
) -> None:
    code = await lobby(conn, world)
    outsider = await create_user(conn, "Siswa Kelas Lain")
    await add_membership(conn, world.school_id, outsider, "student")
    async with api_client(conn) as api:
        response = await api.post(
            "/student/runs/join", json={"join_code": code}, headers=as_user(outsider)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (403, "NOT_ENROLLED")


async def test_another_schools_student_gets_404_join_code_invalid(
    conn: asyncpg.Connection, world: World
) -> None:
    code, other = await lobby(conn, world), await build_world(conn, "SMP Lain")
    async with api_client(conn) as api:
        response = await api.post(
            "/student/runs/join", json={"join_code": code}, headers=as_user(other.student_id)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (404, "JOIN_CODE_INVALID")


async def test_a_used_attempt_is_409_attempt_already_used(
    conn: asyncpg.Connection, world: World
) -> None:
    code = await lobby(conn, world)
    async with api_client(conn) as api:
        response = await api.post(
            "/student/runs/join", json={"join_code": code}, headers=as_user(world.student_id)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "ATTEMPT_ALREADY_USED",
    )


async def test_a_second_different_warmup_choice_is_409(
    conn: asyncpg.Connection, world: World
) -> None:
    code, student = await lobby(conn, world), await new_student(conn, world)
    async with api_client(conn) as api:
        await api.post("/student/runs/join", json={"join_code": code}, headers=as_user(student))
        url = f"/student/runs/{world.run_id}/warmup-choice"
        first = await api.put(url, json={"choice_id": "a"}, headers=as_user(student))
        again = await api.put(url, json={"choice_id": "a"}, headers=as_user(student))
        other = await api.put(url, json={"choice_id": "b"}, headers=as_user(student))
    assert first.json()["scored"] is False
    assert again.json()["submitted_at"] == first.json()["submitted_at"]
    assert (other.status_code, other.json()["error"]["code"]) == (
        409,
        "WARMUP_ALREADY_SUBMITTED",
    )


async def test_window_session_is_created_once(conn: asyncpg.Connection, world: World) -> None:
    _, version_id = await create_mission_version(
        conn, world.school_id, world.kb_id, world.teacher_id, context_pack=world.pack
    )
    publication_id = await publish(
        conn, world.school_id, version_id, world.class_id, world.teacher_id
    )
    await create_run(conn, world.school_id, publication_id, mode="window", status="open")
    student = await new_student(conn, world)
    async with api_client(conn) as api:
        url = f"/student/publications/{publication_id}/window-session"
        first = await api.post(url, headers=as_user(student))
        second = await api.post(url, headers=as_user(student))
    assert first.status_code == 201
    assert first.json()["session_id"] == second.json()["session_id"]
    assert first.json()["prompt"] == {
        "kind": "anchor",
        "text": "Kenapa kelereng berhenti?",
        "turn_index": 0,
    }


async def test_student_missions_show_the_attempt_status(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        body = (await api.get("/student/missions", headers=as_user(world.student_id))).json()
    [card] = body["upcoming"] + body["open"] + body["completed"]
    assert card["publication_id"] == str(world.publication_id)
    assert card["attempt_status"] == "in_progress"
    assert card["session_id"] == str(world.session_id)
    assert set(card) == {
        "publication_id",
        "session_id",
        "mission_title",
        "subject_name",
        "mode",
        "opens_at",
        "closes_at",
        "run_status",
        "attempt_status",
        "target_duration_minutes",
        "max_duration_minutes",
    }
