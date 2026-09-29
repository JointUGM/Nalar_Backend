import asyncio
from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest

from nalar.application.errors import Conflict
from nalar.application.features.runs.commands.close_run import CloseRun, CloseRunHandler
from nalar.application.features.runs.commands.open_lobby import (
    JoinCodes,
    OpenLobby,
    OpenLobbyHandler,
)
from nalar.application.features.runs.commands.start_run import StartRun, StartRunHandler
from nalar.application.features.runs.commands.warm_run import WarmRunHandler
from nalar.application.ports.ai import AiServiceError
from nalar.domain import join_code
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    add_waiting_students,
    build_world,
    create_mission_version,
    create_run,
    open_lobby_run,
    publish,
)
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import (
    FakeClock,
    RecordingBackground,
    ScriptedAiGateway,
    invocation,
)


class ScriptedCodes(JoinCodes):
    def __init__(self, codes: list[str]) -> None:
        self._codes = iter(codes)

    def next(self) -> str:
        return next(self._codes)


async def test_open_lobby_issues_a_code_and_repeats_it(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        first = await api.post(
            f"/runs/{world.run_id}/open-lobby", headers=as_user(world.teacher_id)
        )
        second = await api.post(
            f"/runs/{world.run_id}/open-lobby", headers=as_user(world.teacher_id)
        )
    assert first.status_code == 200
    assert first.json()["status"] == "lobby"
    assert first.json()["join_code"] == second.json()["join_code"]


async def test_join_code_collision_retries(conn: asyncpg.Connection, world: World) -> None:
    other = await build_world(conn, "SMP Lain")
    taken = join_code.generate()
    await open_lobby_run(conn, other, taken)
    fresh = next(c for c in iter(join_code.generate, None) if c != taken)
    handler = OpenLobbyHandler(uow_on(conn), FakeClock(), ScriptedCodes([taken, fresh]))
    result = await handler.execute(OpenLobby(world.teacher_id, world.run_id))
    assert result.join_code == fresh


async def test_open_lobby_on_a_window_run_is_409(conn: asyncpg.Connection, world: World) -> None:
    _, version_id = await create_mission_version(
        conn, world.school_id, world.kb_id, world.teacher_id, context_pack=world.pack
    )
    publication_id = await publish(
        conn, world.school_id, version_id, world.class_id, world.teacher_id
    )
    run_id = await create_run(conn, world.school_id, publication_id, mode="window", status="open")
    handler = OpenLobbyHandler(uow_on(conn), FakeClock(), JoinCodes())
    with pytest.raises(Conflict) as caught:
        await handler.execute(OpenLobby(world.teacher_id, run_id))
    assert caught.value.code == "RUN_STATE_CONFLICT"


async def test_start_creates_one_session_and_anchor_per_waiting_participant(
    conn: asyncpg.Connection, world: World
) -> None:
    await open_lobby_run(conn, world, join_code.generate())
    students = await add_waiting_students(conn, world, 3)
    clock, background = FakeClock(), RecordingBackground()
    handler = StartRunHandler(uow_on(conn), clock, background)
    result = await handler.execute(StartRun(world.teacher_id, world.run_id))
    rows = await conn.fetch(
        "select s.started_at, s.deadline_at, t.turn_index, t.prompt_kind::text as kind"
        " from sessions s join session_turns t on t.session_id = s.id"
        " where s.run_id = $1 and s.student_id = any($2::uuid[])",
        world.run_id,
        students,
    )
    started = await conn.fetchval(
        "select count(*) from run_participants where run_id = $1 and status = 'started'"
        " and session_id is not null",
        world.run_id,
    )
    assert result.started_count == started == 3
    assert len(rows) == 3
    assert {r["started_at"] for r in rows} == {clock.now()}
    assert {r["deadline_at"] for r in rows} == {clock.now() + timedelta(minutes=20)}
    assert {(r["turn_index"], r["kind"]) for r in rows} == {(0, "anchor")}
    assert background.warmed == [world.run_id]


async def test_concurrent_start_creates_one_session_per_participant(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as c:
        world = await build_world(c, f"SMP Paralel {uuid4().hex[:6]}")
        await open_lobby_run(c, world, join_code.generate())
        students = await add_waiting_students(c, world, 8)
    command = StartRun(world.teacher_id, world.run_id)

    def handler() -> StartRunHandler:
        return StartRunHandler(PgUnitOfWork(pool.acquire), FakeClock(), RecordingBackground())

    first, second = await asyncio.gather(handler().execute(command), handler().execute(command))
    assert first.started_at == second.started_at
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select student_id, count(*) as n from sessions where run_id = $1"
            " and student_id = any($2::uuid[]) group by student_id",
            world.run_id,
            students,
        )
    assert {r["student_id"] for r in rows} == set(students)
    assert {r["n"] for r in rows} == {1}


async def test_close_from_lobby_cancels_waiting_participants(
    conn: asyncpg.Connection, world: World
) -> None:
    await open_lobby_run(conn, world, join_code.generate())
    await add_waiting_students(conn, world, 2)
    handler = CloseRunHandler(uow_on(conn), FakeClock())
    result = await handler.execute(CloseRun(world.teacher_id, world.run_id))
    statuses = await conn.fetch(
        "select status::text from run_participants where run_id = $1", world.run_id
    )
    assert {r["status"] for r in statuses} == {"cancelled"}
    assert result.closed_at is not None


async def test_warm_failure_is_swallowed_and_its_invocations_kept(
    conn: asyncpg.Connection, world: World
) -> None:
    ai = ScriptedAiGateway()
    ai.script(
        "warm_run",
        AiServiceError("upstream_unavailable", 503, [invocation("probe_plan", "error")]),
    )
    count = "select count(*) from ai_invocations where school_id = $1"
    before = await conn.fetchval(count, world.school_id)
    await WarmRunHandler(uow_on(conn), ai).execute(world.run_id)
    assert await conn.fetchval(count, world.school_id) == before + 1
    assert [method for method, _ in ai.calls] == ["warm_run"]


async def test_another_teacher_cannot_control_the_run(
    conn: asyncpg.Connection, world: World
) -> None:
    other = await build_world(conn, "SMP Lain")
    async with api_client(conn) as api:
        for action in ("open-lobby", "start", "close"):
            response = await api.post(
                f"/runs/{world.run_id}/{action}", headers=as_user(other.teacher_id)
            )
            assert response.status_code == 404
