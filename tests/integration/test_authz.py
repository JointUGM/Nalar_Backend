from uuid import uuid4

import asyncpg

from nalar.infrastructure.db.authz import PgAuthz
from tests.integration.support.factories import (
    World,
    build_world,
    create_class,
    create_subject,
    create_teacher,
)


async def test_assigned_teacher_teaches_class_publication_run_and_session(
    conn: asyncpg.Connection, world: World
) -> None:
    authz = PgAuthz(conn)
    assert await authz.teaches_class(world.teacher_id, world.class_id)
    assert await authz.teaches_class_subject(world.teacher_id, world.class_id, world.subject_id)
    assert await authz.teaches_publication(world.teacher_id, world.publication_id)
    assert await authz.teaches_run(world.teacher_id, world.run_id)
    assert await authz.teaches_session(world.teacher_id, world.session_id)


async def test_teacher_from_another_school_is_denied_everything(
    conn: asyncpg.Connection, world: World
) -> None:
    other = await build_world(conn, "SMP Lain")
    authz = PgAuthz(conn)
    assert not await authz.teaches_class(other.teacher_id, world.class_id)
    assert not await authz.teaches_publication(other.teacher_id, world.publication_id)
    assert not await authz.teaches_run(other.teacher_id, world.run_id)
    assert not await authz.teaches_session(other.teacher_id, world.session_id)
    assert not await authz.can_read_kb(other.teacher_id, world.kb_id)
    assert not await authz.owns_kb(other.teacher_id, world.kb_id)
    assert not await authz.is_mission_creator(other.teacher_id, world.mission_id)


async def test_inactive_teacher_is_denied(conn: asyncpg.Connection, world: World) -> None:
    await conn.execute(
        "update school_memberships set status = 'inactive' where user_id = $1", world.teacher_id
    )
    authz = PgAuthz(conn)
    assert not await authz.teaches_class(world.teacher_id, world.class_id)
    assert not await authz.teaches_publication(world.teacher_id, world.publication_id)
    assert not await authz.owns_kb(world.teacher_id, world.kb_id)


async def test_teacher_of_another_subject_does_not_teach_the_publication(
    conn: asyncpg.Connection, world: World
) -> None:
    colleague = await create_teacher(conn, world.school_id)
    maths = await create_subject(conn, world.school_id, "Matematika")
    await conn.execute(
        "insert into teaching_assignments (school_id, class_id, school_subject_id, teacher_id)"
        " values ($1, $2, $3, $4)",
        world.school_id,
        world.class_id,
        maths,
        colleague,
    )
    authz = PgAuthz(conn)
    assert await authz.teaches_class(colleague, world.class_id)
    assert not await authz.teaches_class_subject(colleague, world.class_id, world.subject_id)
    assert not await authz.teaches_publication(colleague, world.publication_id)
    assert not await authz.can_read_kb(colleague, world.kb_id)


async def test_subject_colleague_reads_but_does_not_own_the_kb(
    conn: asyncpg.Connection, world: World
) -> None:
    colleague = await create_teacher(conn, world.school_id)
    class_b = await create_class(conn, world.school_id, world.year_id, "8B")
    await conn.execute(
        "insert into teaching_assignments (school_id, class_id, school_subject_id, teacher_id)"
        " values ($1, $2, $3, $4)",
        world.school_id,
        class_b,
        world.subject_id,
        colleague,
    )
    authz = PgAuthz(conn)
    assert await authz.can_read_kb(colleague, world.kb_id)
    assert not await authz.owns_kb(colleague, world.kb_id)
    assert await authz.owns_kb(world.teacher_id, world.kb_id)
    assert await authz.is_mission_creator(world.teacher_id, world.mission_id)
    assert not await authz.is_mission_creator(colleague, world.mission_id)


async def test_student_parent_and_admin_scopes(conn: asyncpg.Connection, world: World) -> None:
    other = await build_world(conn, "SMP Lain")
    authz = PgAuthz(conn)
    assert await authz.is_enrolled(world.student_id, world.class_id)
    assert not await authz.is_enrolled(other.student_id, world.class_id)
    assert await authz.owns_session(world.student_id, world.session_id)
    assert not await authz.owns_session(other.student_id, world.session_id)
    assert await authz.is_linked_parent(world.parent_id, world.student_id)
    assert not await authz.is_linked_parent(other.parent_id, world.student_id)
    assert await authz.is_school_admin(world.admin_id, world.school_id)
    assert not await authz.is_school_admin(world.teacher_id, world.school_id)
    assert not await authz.teaches_class(world.student_id, world.class_id)


async def test_inactive_student_is_not_enrolled(conn: asyncpg.Connection, world: World) -> None:
    await conn.execute(
        "update school_memberships set status = 'inactive' where user_id = $1", world.student_id
    )
    authz = PgAuthz(conn)
    assert not await authz.is_enrolled(world.student_id, world.class_id)
    assert not await authz.owns_session(world.student_id, world.session_id)


async def test_unknown_ids_are_denied(conn: asyncpg.Connection, world: World) -> None:
    authz = PgAuthz(conn)
    assert not await authz.teaches_publication(world.teacher_id, uuid4())
    assert not await authz.can_read_kb(world.teacher_id, uuid4())


async def test_school_teacher_membership(conn: asyncpg.Connection, world: World) -> None:
    other = await build_world(conn, "SMP Lain")
    authz = PgAuthz(conn)
    assert await authz.is_school_teacher(world.teacher_id, world.school_id)
    assert not await authz.is_school_teacher(other.teacher_id, world.school_id)
    assert not await authz.is_school_teacher(world.student_id, world.school_id)
