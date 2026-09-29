from contextlib import nullcontext

import asyncpg
import pytest

from nalar.infrastructure.db.uow import PgUnitOfWork


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
