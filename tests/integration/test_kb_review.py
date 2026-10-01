import asyncio
from uuid import UUID

import asyncpg
import pytest

from nalar.application.features.knowledge_base.commands.review_item import (
    ReviewItem,
    ReviewItemHandler,
)
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import EmbedIn, EmbedOut
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    create_concept,
    create_knowledge_base,
    create_misconception,
    create_school,
    create_subject,
    create_teacher,
)
from tests.unit.application.fakes import FakeClock, ScriptedAiGateway, invocation, vector

MODEL = Settings().embedding_model


async def pending_pair(conn: asyncpg.Connection, world: World) -> tuple[UUID, UUID]:
    concept = await create_concept(conn, world.school_id, world.kb_id, review_status="pending")
    misconception = await create_misconception(conn, world.school_id, world.kb_id, concept)
    return concept, misconception


async def test_misconception_needs_its_concept_approved_first(
    conn: asyncpg.Connection, world: World
) -> None:
    concept, misconception = await pending_pair(conn, world)
    approve = {"review_status": "approved"}
    async with api_client(conn) as api:
        early = await api.post(
            f"/misconceptions/{misconception}/review",
            json=approve,
            headers=as_user(world.teacher_id),
        )
        first = await api.post(
            f"/concepts/{concept}/review", json=approve, headers=as_user(world.teacher_id)
        )
        later = await api.post(
            f"/misconceptions/{misconception}/review",
            json=approve,
            headers=as_user(world.teacher_id),
        )
    assert (early.status_code, early.json()["error"]["code"]) == (409, "CONCEPT_NOT_APPROVED")
    assert first.status_code == later.status_code == 200
    assert later.json()["review_status"] == "approved"


async def test_same_decision_is_idempotent_and_a_change_conflicts(
    conn: asyncpg.Connection, world: World
) -> None:
    concept, _ = await pending_pair(conn, world)
    async with api_client(conn) as api:
        first = await api.post(
            f"/concepts/{concept}/review",
            json={"review_status": "rejected"},
            headers=as_user(world.teacher_id),
        )
        again = await api.post(
            f"/concepts/{concept}/review",
            json={"review_status": "rejected"},
            headers=as_user(world.teacher_id),
        )
        flip = await api.post(
            f"/concepts/{concept}/review",
            json={"review_status": "approved"},
            headers=as_user(world.teacher_id),
        )
    assert first.json() == again.json()
    assert (flip.status_code, flip.json()["error"]["code"]) == (409, "ITEM_NOT_PENDING")
    assert (
        await conn.fetchval(
            "select count(*) from audit_logs where action = 'kb.item_reviewed' and entity_id = $1",
            concept,
        )
        == 1
    )


async def test_only_the_owner_reviews(conn: asyncpg.Connection, world: World) -> None:
    concept, _ = await pending_pair(conn, world)
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
        response = await api.post(
            f"/concepts/{concept}/review",
            json={"review_status": "approved"},
            headers=as_user(colleague),
        )
    assert (response.status_code, response.json()["error"]["code"]) == (403, "NOT_OWNER")


async def test_patch_re_embeds_and_records_the_model(
    conn: asyncpg.Connection, world: World
) -> None:
    concept, _ = await pending_pair(conn, world)
    ai = ScriptedAiGateway()
    ai.script(
        "embed",
        AiResult(
            EmbedOut(dimensions=1536, embedding_model=MODEL, vectors=[vector(5)]),
            [invocation("embedding")],
        ),
    )
    async with api_client(conn, ai=ai) as api:
        response = await api.patch(
            f"/concepts/{concept}",
            json={"name": "Gaya gesek statis", "description": "Menahan benda tetap diam."},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 200
    assert response.json()["name"] == "Gaya gesek statis"
    row = await conn.fetchrow(
        "select embedding_model, embedding = $2 as same from concepts where id = $1",
        concept,
        vector(5),
    )
    assert row is not None
    assert (row["embedding_model"], row["same"]) == (MODEL, True)
    body = ai.calls[0][1]
    assert isinstance(body, EmbedIn)
    assert body.texts[0].root == "Gaya gesek statis: Menahan benda tetap diam."


async def test_embedding_failure_saves_nothing(conn: asyncpg.Connection, world: World) -> None:
    concept, _ = await pending_pair(conn, world)
    ai = ScriptedAiGateway()
    ai.script(
        "embed", AiServiceError("upstream_unavailable", 503, [invocation("embedding", "error")])
    )
    async with api_client(conn, ai=ai) as api:
        response = await api.patch(
            f"/concepts/{concept}", json={"name": "Nama baru"}, headers=as_user(world.teacher_id)
        )
    assert response.status_code == 503
    assert await conn.fetchval("select name from concepts where id = $1", concept) == "Gaya gesek"
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1 and status = 'failed'",
            world.school_id,
        )
        == 1
    )


async def test_approved_items_cannot_be_patched(conn: asyncpg.Connection, world: World) -> None:
    async with api_client(conn) as api:
        response = await api.patch(
            f"/concepts/{world.concept_id}", json={"name": "x"}, headers=as_user(world.teacher_id)
        )
    assert (response.status_code, response.json()["error"]["code"]) == (409, "ITEM_NOT_PENDING")


async def test_review_queue_counts_pending_items(conn: asyncpg.Connection, world: World) -> None:
    await pending_pair(conn, world)
    async with api_client(conn) as api:
        response = await api.get(
            f"/knowledge-bases/{world.kb_id}/review-queue", headers=as_user(world.teacher_id)
        )
    assert response.json() == {
        "knowledge_base_id": str(world.kb_id),
        "pending_concepts": 1,
        "pending_misconceptions": 1,
    }


@pytest.mark.parametrize("kind", ["concepts", "misconceptions"])
async def test_non_owner_cannot_edit_or_read_review_queue(
    conn: asyncpg.Connection, world: World, kind: str
) -> None:
    concept, misconception = await pending_pair(conn, world)
    colleague = await create_teacher(conn, world.school_id)
    await conn.execute(
        "insert into teaching_assignments (school_id, class_id, school_subject_id, teacher_id)"
        " values ($1, $2, $3, $4)",
        world.school_id,
        world.class_id,
        world.subject_id,
        colleague,
    )
    item = concept if kind == "concepts" else misconception
    patch = {"name": "x"} if kind == "concepts" else {"statement": "x"}
    ai = ScriptedAiGateway()
    async with api_client(conn, ai=ai) as api:
        edit = await api.patch(f"/{kind}/{item}", json=patch, headers=as_user(colleague))
        queue = await api.get(
            f"/knowledge-bases/{world.kb_id}/review-queue", headers=as_user(colleague)
        )
    assert (edit.status_code, edit.json()["error"]["code"]) == (403, "NOT_OWNER")
    assert (queue.status_code, queue.json()["error"]["code"]) == (403, "NOT_OWNER")
    assert ai.calls == []


async def test_misconception_edit_embeds_statement_and_preserves_sources(
    conn: asyncpg.Connection, world: World
) -> None:
    _, misconception = await pending_pair(conn, world)
    ai = ScriptedAiGateway()
    ai.script(
        "embed",
        AiResult(
            EmbedOut(dimensions=1536, embedding_model=MODEL, vectors=[vector(6)]),
            [invocation("embedding")],
        ),
    )
    async with api_client(conn, ai=ai) as api:
        response = await api.patch(
            f"/misconceptions/{misconception}",
            json={
                "statement": "Gaya harus selalu bekerja",
                "detection_cues": ["selalu"],
                "counter_examples": ["Benda di ruang angkasa."],
            },
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 200
    assert response.json()["source_chunk_ids"] == []
    assert response.json()["detection_cues"] == ["selalu"]
    assert await conn.fetchval(
        "select embedding_model = $2 and embedding = $3 from misconceptions where id = $1",
        misconception,
        MODEL,
        vector(6),
    )
    body = ai.calls[0][1]
    assert isinstance(body, EmbedIn)
    assert body.tag == "misconception"
    assert body.texts[0].root == "Gaya harus selalu bekerja"
    assert (
        await conn.fetchval(
            "select count(*) from audit_logs where action = 'kb.item_edited' and entity_id = $1",
            misconception,
        )
        == 1
    )


async def test_correct_understanding_edit_needs_no_statement_embedding(
    conn: asyncpg.Connection, world: World
) -> None:
    _, misconception = await pending_pair(conn, world)
    ai = ScriptedAiGateway()
    async with api_client(conn, ai=ai) as api:
        response = await api.patch(
            f"/misconceptions/{misconception}",
            json={"correct_understanding": "Gerak tidak selalu memerlukan gaya."},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 200
    assert response.json()["correct_understanding"] == "Gerak tidak selalu memerlukan gaya."
    assert ai.calls == []


@pytest.mark.parametrize("bad", ["model", "dimensions", "count", "length", "nonfinite"])
async def test_invalid_embedding_preserves_text_and_paid_invocations(
    conn: asyncpg.Connection, world: World, bad: str
) -> None:
    concept, _ = await pending_pair(conn, world)
    vectors = [vector(1)]
    if bad == "count":
        vectors = []
    elif bad == "length":
        vectors = [[1.0]]
    elif bad == "nonfinite":
        vectors[0][0] = float("nan")
    ai = ScriptedAiGateway()
    ai.script(
        "embed",
        AiResult(
            EmbedOut(
                dimensions=42 if bad == "dimensions" else 1536,
                embedding_model="other" if bad == "model" else MODEL,
                vectors=vectors,
            ),
            [invocation("embedding")],
        ),
    )
    async with api_client(conn, ai=ai) as api:
        response = await api.patch(
            f"/concepts/{concept}",
            json={"name": "Nama baru"},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "EMBEDDING_MODEL_MISMATCH" if bad == "model" else "EMBEDDING_INVALID"
    )
    assert await conn.fetchval("select name from concepts where id = $1", concept) == "Gaya gesek"
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1",
            world.school_id,
        )
        == 1
    )


@pytest.mark.parametrize("race", ["edit", "review", "archive", "revoke"])
async def test_change_during_embedding_does_not_save_stale_edit(
    conn: asyncpg.Connection, world: World, race: str
) -> None:
    concept, _ = await pending_pair(conn, world)

    class RacingAi(ScriptedAiGateway):
        async def embed(self, body: EmbedIn, request_id: str) -> AiResult[EmbedOut]:
            if race == "edit":
                await conn.execute(
                    "update concepts set description = 'Perubahan lain' where id = $1", concept
                )
            elif race == "review":
                async with api_client(conn) as api:
                    review = await api.post(
                        f"/concepts/{concept}/review",
                        json={"review_status": "approved"},
                        headers=as_user(world.teacher_id),
                    )
                assert review.status_code == 200
            elif race == "archive":
                await conn.execute("update concepts set archived_at = now() where id = $1", concept)
            else:
                await conn.execute(
                    "update school_memberships set status = 'inactive'"
                    " where school_id = $1 and user_id = $2 and role = 'teacher'",
                    world.school_id,
                    world.teacher_id,
                )
            return AiResult(
                EmbedOut(dimensions=1536, embedding_model=MODEL, vectors=[vector(4)]),
                [invocation("embedding")],
            )

    async with api_client(conn, ai=RacingAi()) as api:
        response = await api.patch(
            f"/concepts/{concept}",
            json={"name": "Nama baru"},
            headers=as_user(world.teacher_id),
        )
    expected = {
        "edit": (409, "ITEM_CHANGED"),
        "review": (409, "ITEM_NOT_PENDING"),
        "archive": (404, "NOT_FOUND"),
        "revoke": (404, "NOT_FOUND"),
    }
    assert (response.status_code, response.json()["error"]["code"]) == expected[race]
    assert await conn.fetchval("select name from concepts where id = $1", concept) == "Gaya gesek"
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1",
            world.school_id,
        )
        == 1
    )
    assert (
        await conn.fetchval(
            "select count(*) from audit_logs where action = 'kb.item_edited' and entity_id = $1",
            concept,
        )
        == 0
    )


async def test_archived_items_are_hidden_and_excluded_from_review_counts(
    conn: asyncpg.Connection, world: World
) -> None:
    concept, misconception = await pending_pair(conn, world)
    await conn.execute("update concepts set archived_at = now() where id = $1", concept)
    await conn.execute("update misconceptions set archived_at = now() where id = $1", misconception)
    async with api_client(conn) as api:
        for kind, item in [("concepts", concept), ("misconceptions", misconception)]:
            response = await api.post(
                f"/{kind}/{item}/review",
                json={"review_status": "approved"},
                headers=as_user(world.teacher_id),
            )
            assert response.status_code == 404
        queue = await api.get(
            f"/knowledge-bases/{world.kb_id}/review-queue", headers=as_user(world.teacher_id)
        )
    assert queue.json()["pending_concepts"] == queue.json()["pending_misconceptions"] == 0


async def test_concurrent_review_returns_one_decision_and_audit(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        school = await create_school(conn, "SMP Review Race")
        teacher = await create_teacher(conn, school)
        subject = await create_subject(conn, school)
        kb = await create_knowledge_base(conn, school, subject, teacher)
        concept = await create_concept(conn, school, kb, review_status="pending")
    try:
        command = ReviewItem(teacher, "concept", concept, "approved")
        first, second = await asyncio.gather(
            ReviewItemHandler(PgUnitOfWork(pool.acquire), FakeClock()).execute(command),
            ReviewItemHandler(PgUnitOfWork(pool.acquire), FakeClock()).execute(command),
        )
        assert first == second
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "select count(*) from audit_logs"
                    " where action = 'kb.item_reviewed' and entity_id = $1",
                    concept,
                )
                == 1
            )
    finally:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("delete from audit_logs where school_id = $1", school)
            await conn.execute("delete from concepts where knowledge_base_id = $1", kb)
            await conn.execute("delete from knowledge_bases where id = $1", kb)
            await conn.execute("delete from school_subjects where id = $1", subject)
            await conn.execute("delete from school_memberships where school_id = $1", school)
            await conn.execute("delete from profiles where id = $1", teacher)
            await conn.execute("delete from auth.users where id = $1", teacher)
            await conn.execute("delete from schools where id = $1", school)
