import asyncpg
import pytest

from tests.integration.support.factories import (
    World,
    act_as_authenticated,
    create_mission_version,
    create_run,
    publish,
)


async def test_live_run_can_be_scheduled_without_a_join_code(
    conn: asyncpg.Connection, world: World
) -> None:
    status = await conn.fetchval(
        "select status::text from publication_runs where id = $1", world.run_id
    )
    assert status == "scheduled"


async def test_two_lobby_runs_cannot_share_a_join_code(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "update publication_runs set status = 'lobby', join_code = 'ABC234' where id = $1",
        world.run_id,
    )
    _, version_id = await create_mission_version(
        conn, world.school_id, world.kb_id, world.teacher_id
    )
    other_publication = await publish(
        conn, world.school_id, version_id, world.class_id, world.teacher_id
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await create_run(
            conn, world.school_id, other_publication, status="lobby", join_code="ABC234"
        )


async def test_paused_session_has_no_end_time(conn: asyncpg.Connection, world: World) -> None:
    await conn.execute(
        "update sessions set status = 'paused_safety' where id = $1", world.session_id
    )
    status = await conn.fetchval(
        "select status::text from sessions where id = $1", world.session_id
    )
    assert status == "paused_safety"


async def test_completed_session_needs_an_end_time(conn: asyncpg.Connection, world: World) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "update sessions set status = 'completed', end_reason = 'student_completed'"
            " where id = $1",
            world.session_id,
        )


async def test_unreviewed_version_cannot_be_published(
    conn: asyncpg.Connection, world: World
) -> None:
    _, draft_id = await create_mission_version(
        conn, world.school_id, world.kb_id, world.teacher_id, reviewed=False
    )
    with pytest.raises(asyncpg.CheckViolationError, match="reviewed mission version"):
        await publish(conn, world.school_id, draft_id, world.class_id, world.teacher_id)


async def test_student_joins_a_run_once(conn: asyncpg.Connection, world: World) -> None:
    insert = "insert into run_participants (school_id, run_id, student_id) values ($1, $2, $3)"
    await conn.execute(insert, world.school_id, world.run_id, world.student_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(insert, world.school_id, world.run_id, world.student_id)


async def test_students_cannot_read_the_question_bank(
    conn: asyncpg.Connection, world: World
) -> None:
    await act_as_authenticated(conn, world.student_id)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.fetch("select question_bank from mission_versions")


async def test_student_reads_only_their_own_participant_row(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "insert into run_participants (school_id, run_id, student_id) values ($1, $2, $3)",
        world.school_id,
        world.run_id,
        world.student_id,
    )
    await act_as_authenticated(conn, world.student_id)
    own = await conn.fetchval("select count(*) from run_participants")
    await act_as_authenticated(conn, world.parent_id)
    other = await conn.fetchval("select count(*) from run_participants")
    assert (own, other) == (1, 0)


async def test_queues_and_schedules_exist(conn: asyncpg.Connection) -> None:
    queues = {row["queue_name"] for row in await conn.fetch("select * from pgmq.list_queues()")}
    jobs = {row["jobname"] for row in await conn.fetch("select jobname from cron.job")}
    assert {"nalar_kb", "nalar_eval", "nalar_default"} <= queues
    assert {"nalar-tick-minute", "nalar-weekly-digest", "nalar-release-reminders"} <= jobs
