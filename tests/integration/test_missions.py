from typing import Any
from uuid import UUID

import asyncpg

from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import WarmOut
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import RUBRIC, World, create_concept, create_teacher
from tests.unit.application.fakes import ScriptedAiGateway, invocation

WARMED = AiResult(WarmOut(warmed=True), [invocation("probe_plan")])


def version_body(world: World, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "anchor_problem": world.pack["anchor_problem"],
        "rubric": RUBRIC,
        "target_concept_ids": [str(c) for c in world.concept_ids],
        "misconception_ids": [str(m) for m in world.misconception_ids],
        "question_bank": world.pack["question_bank"],
        "answer_terms": world.pack["answer_terms"],
        "reference_reasoning": world.pack["reference_reasoning"],
        "source_chunk_ids": [],
        "max_turns": 6,
    }
    return body | overrides


async def new_mission(api: Any, world: World) -> str:
    response = await api.post(
        "/missions",
        json={
            "knowledge_base_id": str(world.kb_id),
            "title": "Gesekan",
            "learning_objective": "Menjelaskan gaya gesek",
        },
        headers=as_user(world.teacher_id),
    )
    assert response.status_code == 201
    mission_id: str = response.json()["mission_id"]
    return mission_id


async def test_hand_authored_version_reviews_and_publishes(
    conn: asyncpg.Connection, world: World
) -> None:
    ai = ScriptedAiGateway()
    ai.script("warm_run", WARMED)
    headers = as_user(world.teacher_id)
    async with api_client(conn, ai=ai) as api:
        mission_id = await new_mission(api, world)
        created = await api.post(
            f"/missions/{mission_id}/versions", json=version_body(world), headers=headers
        )
        reviewed = await api.post(
            f"/missions/{mission_id}/versions/1/review", json={}, headers=headers
        )
        published = await api.post(
            "/publications",
            json={
                "mission_version_id": created.json()["version_id"],
                "class_id": str(world.class_id),
                "run": {"mode": "live"},
            },
            headers=headers,
        )
        locked = await api.post(
            f"/missions/{mission_id}/versions/1/review", json={}, headers=headers
        )
    assert created.status_code == 201
    assert created.json()["status"] == "draft"
    assert reviewed.json()["status"] == "reviewed"
    assert published.status_code == 201
    assert (locked.status_code, locked.json()["error"]["code"]) == (409, "VERSION_LOCKED")
    pack = await conn.fetchval(
        "select context_pack from mission_versions where id = $1",
        UUID(created.json()["version_id"]),
    )
    assert pack is not None


async def test_invalid_pack_lists_its_problems(conn: asyncpg.Connection, world: World) -> None:
    thin_bank = [q for q in world.pack["question_bank"] if q["move"] == "request_justification"]
    headers = as_user(world.teacher_id)
    async with api_client(conn) as api:
        mission_id = await new_mission(api, world)
        await api.post(
            f"/missions/{mission_id}/versions",
            json=version_body(world, question_bank=thin_bank),
            headers=headers,
        )
        response = await api.post(
            f"/missions/{mission_id}/versions/1/review", json={}, headers=headers
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MISSION_VERSION_INVALID"
    codes = {p["code"] for p in response.json()["error"]["details"]["problems"]}
    assert "MOVE_MISSING" in codes


async def test_ai_rejection_leaves_the_version_a_draft(
    conn: asyncpg.Connection, world: World
) -> None:
    ai = ScriptedAiGateway()
    ai.script("warm_run", AiServiceError("invalid_input", 422, message="pack rejected"))
    headers = as_user(world.teacher_id)
    async with api_client(conn, ai=ai) as api:
        mission_id = await new_mission(api, world)
        created = await api.post(
            f"/missions/{mission_id}/versions", json=version_body(world), headers=headers
        )
        response = await api.post(
            f"/missions/{mission_id}/versions/1/review", json={}, headers=headers
        )
    assert (response.status_code, response.json()["error"]["code"]) == (
        422,
        "MISSION_VERSION_INVALID",
    )
    assert (
        await conn.fetchval(
            "select reviewed_at from mission_versions where id = $1",
            UUID(created.json()["version_id"]),
        )
        is None
    )


async def test_unapproved_target_is_rejected_at_create(
    conn: asyncpg.Connection, world: World
) -> None:
    pending = await create_concept(conn, world.school_id, world.kb_id, review_status="pending")
    targets = [str(world.concept_ids[0]), str(pending)]
    async with api_client(conn) as api:
        mission_id = await new_mission(api, world)
        response = await api.post(
            f"/missions/{mission_id}/versions",
            json=version_body(world, target_concept_ids=targets),
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["problems"] == [
        {"code": "ITEM_NOT_APPROVED", "detail": str(pending)}
    ]


async def test_generation_is_unavailable_until_s2(conn: asyncpg.Connection, world: World) -> None:
    async with api_client(conn) as api:
        mission_id = await new_mission(api, world)
        response = await api.post(
            f"/missions/{mission_id}/generate", json={}, headers=as_user(world.teacher_id)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (
        503,
        "MISSION_GENERATION_UNAVAILABLE",
    )


async def test_colleague_sees_reviewed_versions_but_cannot_author(
    conn: asyncpg.Connection, world: World
) -> None:
    colleague = await create_teacher(conn, world.school_id)
    await conn.execute(
        "insert into teaching_assignments (school_id, class_id, school_subject_id, teacher_id)"
        " values ($1, $2, $3, $4)",
        world.school_id,
        world.class_id,
        world.subject_id,
        colleague,
    )
    async with api_client(conn) as api:
        mission_id = await new_mission(api, world)
        await api.post(
            f"/missions/{mission_id}/versions",
            json=version_body(world),
            headers=as_user(world.teacher_id),
        )
        draft = await api.get(f"/missions/{mission_id}/versions/1", headers=as_user(colleague))
        author = await api.post(
            f"/missions/{mission_id}/versions", json=version_body(world), headers=as_user(colleague)
        )
        listed = await api.get(f"/schools/{world.school_id}/missions", headers=as_user(colleague))
    assert draft.status_code == 404
    assert (author.status_code, author.json()["error"]["code"]) == (403, "NOT_OWNER")
    assert mission_id not in {m["id"] for m in listed.json()["items"]}
    assert str(world.mission_id) in {m["id"] for m in listed.json()["items"]}
