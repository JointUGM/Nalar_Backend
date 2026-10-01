from contextlib import nullcontext
from uuid import uuid4

import asyncpg
import pytest

from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.factories import World


async def test_unit_of_work_commits_on_success_and_rolls_back_on_error(
    conn: asyncpg.Connection,
) -> None:
    await conn.execute("create temp table uow_probe (x int)")
    uow = PgUnitOfWork(lambda: nullcontext(conn))

    async with uow:
        await conn.execute("insert into uow_probe values (1)")
    with pytest.raises(RuntimeError):
        async with uow:
            await conn.execute("insert into uow_probe values (2)")
            raise RuntimeError

    assert [row["x"] for row in await conn.fetch("select x from uow_probe")] == [1]


async def test_failed_work_is_rolled_back_with_its_queue_message(
    conn: asyncpg.Connection, world: World
) -> None:
    jobs_before = await conn.fetchval("select count(*) from jobs")
    probe = "select count(*) from pgmq.q_nalar_default where message->>'kind' = 'rollback_probe'"
    with pytest.raises(RuntimeError):
        async with PgUnitOfWork(lambda: nullcontext(conn)) as uow:
            await uow.jobs.create(
                kind="kb_build_section",
                entity_type="material_section",
                entity_id=uuid4(),
                school_id=world.school_id,
                requested_by=world.teacher_id,
            )
            await uow.queue.send(DEFAULT_QUEUE, {"kind": "rollback_probe"})
            raise RuntimeError("use case failed")
    assert await conn.fetchval("select count(*) from jobs") == jobs_before
    assert await conn.fetchval(probe) == 0
