from contextlib import nullcontext

import asyncpg

from nalar.infrastructure.db.uow import PgUnitOfWork


def uow_on(conn: asyncpg.Connection) -> PgUnitOfWork:
    return PgUnitOfWork(lambda: nullcontext(conn))
