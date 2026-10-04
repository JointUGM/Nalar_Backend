from uuid import uuid4

import asyncpg

from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import EmbedOut
from nalar.bootstrap.settings import Settings
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world
from tests.unit.application.fakes import ScriptedAiGateway, invocation, vector


async def test_manual_items_are_pending_once_and_keep_the_approval_dependency(
    conn: asyncpg.Connection, world: World
) -> None:
    ai = ScriptedAiGateway()
    for _ in range(2):
        ai.script(
            "embed",
            AiResult(
                EmbedOut(
                    dimensions=1536, embedding_model=Settings().embedding_model, vectors=[vector(5)]
                ),
                [invocation("embedding")],
            ),
        )
    url = f"/knowledge-bases/{world.kb_id}/concepts"
    headers = as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())}
    body = {"name": "Konsep manual", "description": "Penjelasan guru"}
    async with api_client(conn, ai=ai) as api:
        first = await api.post(url, json=body, headers=headers)
        assert first.status_code == 201, first.text
        repeated = await api.post(url, json=body, headers=headers)
        assert repeated.json() == first.json() and len(ai.calls) == 1
        assert first.json()["review_status"] == "pending"
        concept = first.json()["id"]
        assert (
            await conn.fetchval("select origin::text from concepts where id = $1", concept)
            == "teacher"
        )
        misconception = await api.post(
            f"{url}/{concept}/misconceptions",
            json={"statement": "Kesalahan", "correct_understanding": "Pemahaman tepat"},
            headers=as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())},
        )
        assert misconception.status_code == 201, misconception.text
        early = await api.post(
            f"/misconceptions/{misconception.json()['id']}/review",
            headers=as_user(world.teacher_id),
            json={"review_status": "approved"},
        )
        assert early.status_code == 409
        approved = await api.post(
            f"/concepts/{concept}/review",
            headers=as_user(world.teacher_id),
            json={"review_status": "approved"},
        )
        assert approved.status_code == 200
        assert (
            await api.post(
                f"/misconceptions/{misconception.json()['id']}/review",
                headers=as_user(world.teacher_id),
                json={"review_status": "approved"},
            )
        ).status_code == 200


async def test_manual_creation_rejects_foreign_references_before_ai(
    conn: asyncpg.Connection, world: World
) -> None:
    foreign = await build_world(conn, "Other school")
    ai = ScriptedAiGateway()
    async with api_client(conn, ai=ai) as api:
        headers = as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())}
        rejected = await api.post(
            f"/knowledge-bases/{world.kb_id}/concepts/{foreign.concept_id}/misconceptions",
            json={"statement": "Kesalahan", "correct_understanding": "Pemahaman"},
            headers=headers,
        )
        assert rejected.status_code == 404 and not ai.calls
        rejected = await api.post(
            f"/knowledge-bases/{foreign.kb_id}/concepts",
            json={"name": "Konsep"},
            headers=headers,
        )
        assert rejected.status_code == 404 and not ai.calls


async def test_manual_embedding_errors_persist_provenance_without_creating_content(
    conn: asyncpg.Connection, world: World
) -> None:
    ai = ScriptedAiGateway()
    ai.script("embed", AiServiceError("timeout", 503, [invocation("embedding", "error")]))
    async with api_client(conn, ai=ai) as api:
        failed = await api.post(
            f"/knowledge-bases/{world.kb_id}/concepts",
            json={"name": "Unwritten"},
            headers=as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())},
        )
        assert failed.status_code == 503
        assert (
            await conn.fetchval(
                "select count(*) from ai_invocations where school_id = $1 and status = 'failed'",
                world.school_id,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "select count(*) from concepts where knowledge_base_id = $1 and name = 'Unwritten'",
                world.kb_id,
            )
            == 0
        )
