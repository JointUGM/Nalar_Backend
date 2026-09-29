from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from nalar.infrastructure.db.uow import PgUnitOfWork


class DroppedTransaction:
    async def start(self) -> None:
        raise ConnectionError("connection dropped")


class DroppedConnection:
    def transaction(self) -> DroppedTransaction:
        return DroppedTransaction()


async def test_connection_is_released_when_the_transaction_cannot_start() -> None:
    released: list[bool] = []

    @asynccontextmanager
    async def acquire() -> AsyncIterator[DroppedConnection]:
        try:
            yield DroppedConnection()
        finally:
            released.append(True)

    uow = PgUnitOfWork(cast(Any, acquire))
    with pytest.raises(ConnectionError):
        async with uow:
            pass
    assert released == [True]
