from uuid import uuid4

import asyncpg
import pytest

from tests.integration.support.factories import World, act_as_authenticated

TEACHER_VIEWS = [
    "v_teacher_session_turns",
    "v_session_challenge_coverage",
    "v_teacher_mission_versions",
]

HELPER_CALLS = [
    "select public.has_school_role($1, array['teacher']::membership_role[])",
    "select public.teaches_publication($1)",
    "select public.parent_can_see_session($1)",
    "select public.parent_can_see_publication($1, $1)",
]

TRIGGER_FUNCTIONS = [
    "set_updated_at",
    "prevent_locked_version_update",
    "lock_version_on_publish",
    "apply_score_override",
]


@pytest.mark.parametrize("view", TEACHER_VIEWS)
async def test_browser_teachers_cannot_read_security_barrier_views(
    conn: asyncpg.Connection, world: World, view: str
) -> None:
    await act_as_authenticated(conn, world.teacher_id)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.fetch(f"select * from public.{view}")


@pytest.mark.parametrize("call", HELPER_CALLS)
async def test_anonymous_callers_cannot_run_helper_functions(
    conn: asyncpg.Connection, call: str
) -> None:
    await conn.execute("set local role anon")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.fetchval(call, uuid4())


async def test_anonymous_callers_cannot_run_is_platform_admin(conn: asyncpg.Connection) -> None:
    await conn.execute("set local role anon")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.fetchval("select public.is_platform_admin()")


async def test_signed_in_policies_still_evaluate_the_helpers(
    conn: asyncpg.Connection, world: World
) -> None:
    await act_as_authenticated(conn, world.student_id)
    own_sessions = await conn.fetchval("select count(*) from sessions")
    await act_as_authenticated(conn, world.teacher_id)
    taught_sessions = await conn.fetchval("select count(*) from sessions")
    assert (own_sessions, taught_sessions) == (1, 1)


@pytest.mark.parametrize("function", TRIGGER_FUNCTIONS)
async def test_trigger_functions_pin_their_search_path(
    conn: asyncpg.Connection, function: str
) -> None:
    config = await conn.fetchval(
        "select proconfig from pg_proc"
        " where proname = $1 and pronamespace = 'public'::regnamespace",
        function,
    )
    assert config is not None and "search_path=public" in config
