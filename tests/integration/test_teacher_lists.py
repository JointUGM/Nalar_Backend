import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, create_class, create_teacher


async def test_assignments_list_the_callers_active_assignments(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        response = await api.get("/teacher/assignments", headers=as_user(world.teacher_id))
    items = response.json()["items"]
    assert [(i["class_id"], i["school_subject_id"]) for i in items] == [
        (str(world.class_id), str(world.subject_id))
    ]


async def test_publications_list_counts_for_assigned_classes_only(
    conn: asyncpg.Connection, world: World
) -> None:
    colleague = await create_teacher(conn, world.school_id)
    async with api_client(conn) as api:
        mine = await api.get("/teacher/publications", headers=as_user(world.teacher_id))
        theirs = await api.get("/teacher/publications", headers=as_user(colleague))
    [item] = mine.json()["items"]
    assert item["id"] == str(world.publication_id)
    assert item["counts"] == {"started": 1, "completed": 0, "timed_out": 0, "evaluated": 0}
    assert theirs.json()["items"] == []


async def test_filtering_by_an_unassigned_class_is_404(
    conn: asyncpg.Connection, world: World
) -> None:
    other_class = await create_class(conn, world.school_id, world.year_id, "8B")
    async with api_client(conn) as api:
        response = await api.get(
            "/teacher/publications",
            params={"class_id": str(other_class)},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 404
