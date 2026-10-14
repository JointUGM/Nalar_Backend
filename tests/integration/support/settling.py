from nalar.infrastructure.db.pool import DbConnection
from tests.integration.support.factories import World, evaluate, finish_session


async def close_runs(conn: DbConnection, world: World) -> None:
    await conn.execute(
        "update publication_runs set status = 'closed', closed_at = now(),"
        " join_code = coalesce(join_code, upper(substr(md5(random()::text), 1, 6)))"
        " where publication_id = $1",
        world.publication_id,
    )


async def settled(conn: DbConnection, world: World) -> None:
    await finish_session(conn, world, world.session_id)
    await evaluate(conn, world, world.session_id)
    await close_runs(conn, world)
