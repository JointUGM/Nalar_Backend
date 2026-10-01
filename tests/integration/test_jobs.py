import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, create_teacher


async def test_only_the_requester_reads_a_job(conn: asyncpg.Connection, world: World) -> None:
    job_id = await conn.fetchval(
        "insert into jobs (school_id, kind, entity_type, entity_id, requested_by)"
        " values ($1, 'kb_detect_sections', 'teaching_materials', gen_random_uuid(), $2)"
        " returning id",
        world.school_id,
        world.teacher_id,
    )
    colleague = await create_teacher(conn, world.school_id)
    async with api_client(conn) as api:
        mine = await api.get(f"/jobs/{job_id}", headers=as_user(world.teacher_id))
        theirs = await api.get(f"/jobs/{job_id}", headers=as_user(colleague))
    assert mine.status_code == 200
    assert set(mine.json()) == {
        "id",
        "kind",
        "status",
        "entity_type",
        "entity_id",
        "error_code",
        "updated_at",
    }
    assert theirs.status_code == 404
