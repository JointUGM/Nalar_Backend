import json
from uuid import UUID

import asyncpg

from nalar.application.features.missions.commands.generate_mission import BuildMissionHandler
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world
from tests.integration.support.uow import uow_on
from tests.integration.test_misconception_trail import Trail
from tests.integration.test_mission_generation import script_success
from tests.integration.test_missions import new_mission
from tests.unit.application.fakes import FakeClock, ScriptedAiGateway


async def generate(conn: asyncpg.Connection, user: UUID, mission: str, body: dict[str, str] | None):  # type: ignore[no-untyped-def]
    async with api_client(conn) as api:
        return await api.post(
            f"/missions/{mission}/generate", json=body or {}, headers=as_user(user)
        )


async def held_trail(conn: asyncpg.Connection, world: World) -> Trail:
    t = await Trail(conn, world).setup()
    for _ in range(2):
        await t.attempt(t.earlier, await t.student(), "misconception", t.held)
    return t


async def test_follow_up_sends_concepts_still_held_to_target_selection(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await held_trail(conn, world)
    async with api_client(conn) as api:
        mission = await new_mission(api, world)
    response = await generate(
        conn, world.teacher_id, mission, {"follow_up_of_publication_id": str(t.earlier)}
    )
    assert response.status_code == 202, response.text
    ai = ScriptedAiGateway()
    script_success(ai, world)
    await BuildMissionHandler(uow_on(conn), ai, FakeClock()).execute(
        UUID(mission), UUID(response.json()["job_id"])
    )
    assert ai.calls[0][1].preferred_concept_ids == [t.concept]  # type: ignore[attr-defined]
    assert "follow_up" not in json.dumps(response.json())


async def test_plain_generation_sends_no_preferred_concepts(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        mission = await new_mission(api, world)
    response = await generate(conn, world.teacher_id, mission, None)
    ai = ScriptedAiGateway()
    script_success(ai, world)
    await BuildMissionHandler(uow_on(conn), ai, FakeClock()).execute(
        UUID(mission), UUID(response.json()["job_id"])
    )
    assert not ai.calls[0][1].preferred_concept_ids  # type: ignore[attr-defined]


async def test_follow_up_of_a_class_without_held_misconceptions_prefers_nothing(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    await t.attempt(t.earlier, await t.student(), "mastered")
    async with api_client(conn) as api:
        mission = await new_mission(api, world)
    response = await generate(
        conn, world.teacher_id, mission, {"follow_up_of_publication_id": str(t.earlier)}
    )
    ai = ScriptedAiGateway()
    script_success(ai, world)
    await BuildMissionHandler(uow_on(conn), ai, FakeClock()).execute(
        UUID(mission), UUID(response.json()["job_id"])
    )
    assert not ai.calls[0][1].preferred_concept_ids  # type: ignore[attr-defined]


async def test_follow_up_of_a_publication_the_teacher_does_not_teach_is_not_found(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await held_trail(conn, world)
    other = await build_world(conn, "SMP Lain")
    async with api_client(conn) as api:
        mission = await new_mission(api, other)
    response = await generate(
        conn, other.teacher_id, mission, {"follow_up_of_publication_id": str(t.earlier)}
    )
    assert response.status_code == 404
    assert await conn.fetchval("select count(*) from jobs where entity_id = $1", UUID(mission)) == 0


async def test_follow_up_must_come_from_the_same_knowledge_base(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await held_trail(conn, world)
    kb2: UUID = await conn.fetchval(
        "insert into knowledge_bases (school_id, school_subject_id, owner_teacher_id, topic_key,"
        " topic_title) values ($1, $2, $3, 'topik-lain', 'Topik Lain') returning id",
        world.school_id,
        world.subject_id,
        world.teacher_id,
    )
    async with api_client(conn) as api:
        created = await api.post(
            "/missions",
            json={"knowledge_base_id": str(kb2), "title": "Lain", "learning_objective": "Tujuan"},
            headers=as_user(world.teacher_id),
        )
        mission = created.json()["mission_id"]
    response = await generate(
        conn, world.teacher_id, mission, {"follow_up_of_publication_id": str(t.earlier)}
    )
    assert response.status_code == 422
    assert "FOLLOW_UP_KB_MISMATCH" in response.text
