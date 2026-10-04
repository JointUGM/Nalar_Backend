from uuid import uuid4

import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world


async def test_import_history_and_csv_are_school_scoped_and_paginated(
    conn: asyncpg.Connection, world: World
) -> None:
    foreign = await build_world(conn, "Other school")
    imports = [uuid4(), uuid4()]
    for import_id in imports:
        await conn.execute(
            "insert into roster_imports(id,school_id,academic_year_id,uploaded_by,storage_path)"
            " values($1,$2,$3,$4,$5)",
            import_id,
            world.school_id,
            world.year_id,
            world.admin_id,
            str(import_id),
        )
    await conn.execute(
        "insert into roster_import_errors(import_id,row_number,field,message)"
        " values($1,2,'email','=DANGEROUS()')",
        imports[0],
    )
    url = f"/schools/{world.school_id}/roster-imports"
    async with api_client(conn) as api:
        first = await api.get(url, params={"limit": 1}, headers=as_user(world.admin_id))
        assert first.status_code == 200, first.text
        assert first.json()["total"] == 2
        second = await api.get(
            url,
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
            headers=as_user(world.admin_id),
        )
        assert first.json()["items"][0]["import_id"] != second.json()["items"][0]["import_id"]
        csv = await api.get(
            f"/roster-imports/{imports[0]}/errors.csv", headers=as_user(world.admin_id)
        )
        assert csv.status_code == 200 and "'=DANGEROUS()" in csv.text
        assert (await api.get(url, headers=as_user(foreign.admin_id))).status_code == 404
        assert (
            await api.get(
                f"/roster-imports/{imports[0]}/errors.csv", headers=as_user(world.parent_id)
            )
        ).status_code == 404
