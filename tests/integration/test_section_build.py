from asyncio import CancelledError
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from pydantic import BaseModel

from nalar.application.features.knowledge_base.commands.build_section import (
    BuildSectionHandler,
    S1Settings,
)
from nalar.application.features.knowledge_base.s1_calls import BuildBusy
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import (
    AlignCpOut,
    ChunkSectionOut,
    DedupeOut,
    ExtractConceptsOut,
    GenerateMisconceptionsOut,
)
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.db.repositories.knowledge import PgKnowledgeRepo
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    add_section,
    create_teacher,
    map_cp_subject,
)
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import (
    FakeClock,
    FakeStorage,
    ScriptedAiGateway,
    invocation,
    vector,
)

MODEL = Settings().embedding_model
S1 = S1Settings(embedding_model=MODEL, default_phase="D", stale_after_s=3600)


def chunked(model: str = MODEL) -> AiResult[ChunkSectionOut]:
    chunks = [
        {
            "local_index": i,
            "content": text,
            "content_sha256": f"h{i}",
            "embedding": vector(10 + i),
            "heading_path": "Bab 1",
            "chunk_kind": "explanation",
            "page_start": 1,
            "page_end": 2,
            "token_count": 20,
        }
        for i, text in enumerate(["Gaya normal tegak lurus bidang.", "Contoh: buku di meja."])
    ]
    return AiResult(
        ChunkSectionOut.model_validate(
            {"chunks": chunks, "embedding_model": model, "skipped_pages": []}
        ),
        [invocation("embedding")],
    )


def extracted(body: BaseModel) -> AiResult[ExtractConceptsOut]:
    first_chunk = str(body.chunks[0].id)  # type: ignore[attr-defined]
    return AiResult(
        ExtractConceptsOut.model_validate(
            {
                "concepts": [
                    {
                        "key": "c1",
                        "name": "Gaya normal",
                        "description": "Gaya tegak lurus bidang.",
                        "embedding": vector(0),
                        "source_chunk_ids": [first_chunk],
                        "prerequisite_keys": [],
                        "prerequisite_existing_ids": [],
                    }
                ],
                "dropped": [],
                "existing_links": [],
                "embedding_model": MODEL,
            }
        ),
        [invocation("kb_extract")],
    )


def deduped(body: BaseModel) -> AiResult[DedupeOut]:
    item = body.items[0]  # type: ignore[attr-defined]
    match = item.candidates[0] if item.candidates else None
    decision: dict[str, Any] = {
        "key": item.key,
        "action": "link" if match else "create",
        "existing_concept_id": str(match.concept_id) if match else None,
        "basis": "similarity" if match else "no_candidate",
        "similarity": match.similarity if match else None,
        "reason": None,
    }
    return AiResult(DedupeOut.model_validate({"decisions": [decision]}), [invocation("kb_dedup")])


def aligned(outcome_id: UUID) -> Any:
    def reply(body: BaseModel) -> AiResult[AlignCpOut]:
        return AiResult(
            AlignCpOut.model_validate(
                {
                    "alignments": [
                        {
                            "concept_ref": i.concept_ref,
                            "outcome_id": str(outcome_id),
                            "basis": "judge",
                            "reason": "cocok",
                        }
                        for i in body.items  # type: ignore[attr-defined]
                    ]
                }
            ),
            [invocation("cp_align")],
        )

    return reply


def generated(body: BaseModel) -> AiResult[GenerateMisconceptionsOut]:
    concept = body.concepts[0]  # type: ignore[attr-defined]
    return AiResult(
        GenerateMisconceptionsOut.model_validate(
            {
                "misconceptions": [
                    {
                        "concept_ref": concept.concept_ref,
                        "statement": "Gaya normal selalu sama dengan berat.",
                        "correct_understanding": "Gaya normal bergantung pada bidang.",
                        "detection_cues": ["selalu sama"],
                        "counter_examples": ["bidang miring"],
                        "embedding": vector(1),
                        "library_id": None,
                        "source_chunk_ids": [str(c) for c in concept.source_chunk_ids],
                    }
                ],
                "dropped": [],
                "failed": [],
                "embedding_model": MODEL,
            }
        ),
        [invocation("kb_misconceptions")],
    )


async def new_job(conn: asyncpg.Connection, world: World, section_id: UUID) -> UUID:
    job_id: UUID = await conn.fetchval(
        "insert into jobs (school_id, kind, entity_type, entity_id, requested_by)"
        " values ($1, 'kb_build_section', 'material_sections', $2, $3) returning id",
        world.school_id,
        section_id,
        world.teacher_id,
    )
    return job_id


def handler(
    conn: asyncpg.Connection, ai: ScriptedAiGateway, storage: FakeStorage
) -> BuildSectionHandler:
    return BuildSectionHandler(uow_on(conn), ai, storage, FakeClock(), S1)


async def ai_concepts(conn: asyncpg.Connection, world: World) -> list[asyncpg.Record]:
    return await conn.fetch(
        "select id, review_status::text, cp_learning_outcome_id from concepts"
        " where knowledge_base_id = $1 and origin = 'ai_generated'",
        world.kb_id,
    )


async def test_rebuild_creates_no_duplicate_chunks_or_concepts(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    ai = ScriptedAiGateway()
    ai.script("chunk_section", chunked())
    ai.script("extract_concepts", extracted, extracted)
    ai.script("dedupe_concepts", deduped, deduped)
    ai.script("generate_misconceptions", generated)
    await handler(conn, ai, storage).execute(section_id, await new_job(conn, world, section_id))
    await handler(conn, ai, storage).execute(section_id, await new_job(conn, world, section_id))
    assert [m for m, _ in ai.calls] == [
        "chunk_section",
        "extract_concepts",
        "dedupe_concepts",
        "generate_misconceptions",
    ]
    assert (
        await conn.fetchval(
            "select count(*) from material_chunks where section_id = $1", section_id
        )
        == 2
    )
    concepts = await ai_concepts(conn, world)
    assert [c["review_status"] for c in concepts] == ["pending"]
    assert (
        await conn.fetchval(
            "select count(*) from misconceptions"
            " where concept_id = $1 and review_status = 'pending'",
            concepts[0]["id"],
        )
        == 1
    )
    assert (
        await conn.fetchval(
            "select build_status::text from material_sections where id = $1", section_id
        )
        == "built"
    )


async def test_rejected_concepts_are_passed_to_extraction(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    await conn.execute(
        "insert into concepts (school_id, knowledge_base_id, name, origin, review_status,"
        " reviewed_at) values ($1, $2, 'Gaya magis', 'ai_generated', 'rejected', now())",
        world.school_id,
        world.kb_id,
    )
    ai = ScriptedAiGateway()
    ai.script("chunk_section", chunked())
    ai.script("extract_concepts", AiServiceError("budget_exceeded", 422))
    await handler(conn, ai, storage).execute(section_id, await new_job(conn, world, section_id))
    body = dict(ai.calls)["extract_concepts"]
    assert [r.name for r in body.rejected_concepts or []] == ["Gaya magis"]  # type: ignore[attr-defined]


async def test_section_fails_after_one_retry_on_502(conn: asyncpg.Connection, world: World) -> None:
    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    job_id = await new_job(conn, world, section_id)
    bad = AiServiceError("ai_output_invalid", 502, [invocation("kb_extract", "error")])
    ai = ScriptedAiGateway()
    ai.script("chunk_section", chunked())
    ai.script("extract_concepts", bad, bad)
    await handler(conn, ai, storage).execute(section_id, job_id)
    assert (
        await conn.fetchval(
            "select build_status::text from material_sections where id = $1", section_id
        )
        == "failed"
    )
    assert await conn.fetchval("select error_code from jobs where id = $1", job_id) == (
        "ai_output_invalid"
    )
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1 and status = 'failed'",
            world.school_id,
        )
        == 2
    )


async def test_build_resumes_after_crash_without_duplicates(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    outcome_id = await map_cp_subject(conn, world, MODEL, vector(0))
    job_id = await new_job(conn, world, section_id)
    ai = ScriptedAiGateway()
    ai.script("chunk_section", chunked())
    ai.script("extract_concepts", extracted, extracted)
    ai.script("dedupe_concepts", deduped, deduped)
    ai.script("align_cp", AiServiceError("upstream_unavailable", 503), aligned(outcome_id))
    ai.script("generate_misconceptions", generated)
    with pytest.raises(AiServiceError):
        await handler(conn, ai, storage).execute(section_id, job_id)
    assert (
        await conn.fetchval(
            "select build_status::text from material_sections where id = $1", section_id
        )
        == "building"
    )
    await handler(conn, ai, storage).execute(section_id, job_id)
    concepts = await ai_concepts(conn, world)
    assert len(concepts) == 1
    assert concepts[0]["cp_learning_outcome_id"] == outcome_id
    assert (
        await conn.fetchval(
            "select count(*) from misconceptions where concept_id = $1", concepts[0]["id"]
        )
        == 1
    )
    assert await conn.fetchval("select status::text from jobs where id = $1", job_id) == "succeeded"


async def test_embedding_model_mismatch_fails_the_section(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    job_id = await new_job(conn, world, section_id)
    ai = ScriptedAiGateway()
    ai.script("chunk_section", chunked(model="another-embedding-model"))
    await handler(conn, ai, storage).execute(section_id, job_id)
    assert await conn.fetchval("select error_code from jobs where id = $1", job_id) == (
        "EMBEDDING_MODEL_MISMATCH"
    )
    assert (
        await conn.fetchval(
            "select count(*) from material_chunks where section_id = $1", section_id
        )
        == 0
    )


async def test_build_route_guards_overlap_and_returns_the_queued_job(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    _, parent = await add_section(conn, world, storage)
    _, child = await add_section(conn, world, storage, parent_id=parent, ordinal=2, level=2)
    _, grandchild = await add_section(conn, world, storage, parent_id=child, ordinal=3, level=3)
    colleague = await create_teacher(conn, world.school_id)
    await conn.execute(
        "insert into teaching_assignments (school_id, class_id, school_subject_id, teacher_id)"
        " values ($1, $2, $3, $4)",
        world.school_id,
        world.class_id,
        world.subject_id,
        colleague,
    )
    path = f"/knowledge-bases/{world.kb_id}/sections/{{}}/build"
    async with api_client(conn, storage=storage) as api:
        first = await api.post(path.format(grandchild), json={}, headers=as_user(world.teacher_id))
        again = await api.post(path.format(grandchild), json={}, headers=as_user(world.teacher_id))
        parent_build = await api.post(
            path.format(parent), json={}, headers=as_user(world.teacher_id)
        )
        not_owner = await api.post(path.format(child), json={}, headers=as_user(colleague))
    assert first.status_code == again.status_code == 202
    assert first.json()["job_id"] == again.json()["job_id"]
    assert (parent_build.status_code, parent_build.json()["error"]["code"]) == (
        409,
        "SECTION_OVERLAP",
    )
    assert (not_owner.status_code, not_owner.json()["error"]["code"]) == (403, "NOT_OWNER")


async def test_stale_lease_fences_old_attempt_and_blocks_parallel_kb_builds(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    _, sibling = await add_section(conn, world, storage, ordinal=2)
    repo = PgKnowledgeRepo(conn)
    ctx = await repo.build_context(section_id, "D")
    other_ctx = await repo.build_context(sibling, "D")
    assert ctx is not None and other_ctx is not None
    job_id = await new_job(conn, world, section_id)
    other_job = await new_job(conn, world, sibling)
    now = await conn.fetchval("select now()")
    attempt = await repo.claim_build(ctx, job_id, now, 3600)
    assert attempt == 1
    with pytest.raises(BuildBusy):
        await repo.claim_build(other_ctx, other_job, now, 3600)
    with pytest.raises(BuildBusy):
        await repo.claim_build(ctx, job_id, now, 3600)
    resumed = await repo.claim_build(ctx, job_id, now + timedelta(hours=2), 3600)
    assert resumed == 2
    assert not await repo.touch_build(job_id, attempt)
    assert await repo.touch_build(job_id, resumed)
    await repo.queue_section(section_id)
    assert not await repo.touch_build(job_id, resumed)


async def test_finished_job_redelivery_makes_no_ai_calls(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    job_id = await new_job(conn, world, section_id)
    await conn.execute("update jobs set status = 'succeeded' where id = $1", job_id)
    ai = ScriptedAiGateway()
    await handler(conn, ai, storage).execute(section_id, job_id)
    assert ai.calls == []


async def test_cancelled_worker_resumes_committed_extraction_after_lease_expires(
    conn: asyncpg.Connection, world: World
) -> None:
    def cancelled(body: BaseModel) -> Any:
        raise CancelledError()

    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    outcome_id = await map_cp_subject(conn, world, MODEL, vector(0))
    job_id = await new_job(conn, world, section_id)
    ai = ScriptedAiGateway()
    ai.script("chunk_section", chunked())
    ai.script("extract_concepts", extracted)
    ai.script("dedupe_concepts", deduped)
    ai.script("align_cp", cancelled, aligned(outcome_id))
    ai.script("generate_misconceptions", generated)
    with pytest.raises(CancelledError):
        await handler(conn, ai, storage).execute(section_id, job_id)
    with pytest.raises(BuildBusy):
        await handler(conn, ai, storage).execute(section_id, job_id)
    clock = FakeClock(await conn.fetchval("select now()") + timedelta(hours=2))
    await BuildSectionHandler(uow_on(conn), ai, storage, clock, S1).execute(section_id, job_id)
    assert [method for method, _ in ai.calls] == [
        "chunk_section",
        "extract_concepts",
        "dedupe_concepts",
        "align_cp",
        "align_cp",
        "generate_misconceptions",
    ]
    assert len(await ai_concepts(conn, world)) == 1
    assert await conn.fetchval("select attempts from jobs where id = $1", job_id) == 2
    assert await conn.fetchval("select status::text from jobs where id = $1", job_id) == "succeeded"


def not_generated(body: BaseModel) -> AiResult[GenerateMisconceptionsOut]:
    refs = [c.concept_ref for c in body.concepts]  # type: ignore[attr-defined]
    return AiResult(
        GenerateMisconceptionsOut.model_validate(
            {
                "misconceptions": [],
                "dropped": [],
                "failed": [{"concept_ref": r, "error": "invalid output"} for r in refs],
                "embedding_model": MODEL,
            }
        ),
        [invocation("kb_misconceptions")],
    )


async def build_with_misconception_replies(
    conn: asyncpg.Connection, world: World, *replies: Any
) -> tuple[UUID, UUID, ScriptedAiGateway]:
    storage = FakeStorage()
    _, section_id = await add_section(conn, world, storage)
    job_id = await new_job(conn, world, section_id)
    ai = ScriptedAiGateway()
    ai.script("chunk_section", chunked())
    ai.script("extract_concepts", extracted)
    ai.script("dedupe_concepts", deduped)
    ai.script("generate_misconceptions", *replies)
    await handler(conn, ai, storage).execute(section_id, job_id)
    return section_id, job_id, ai


async def test_concepts_left_without_misconceptions_are_asked_again(
    conn: asyncpg.Connection, world: World
) -> None:
    section_id, job_id, ai = await build_with_misconception_replies(
        conn, world, not_generated, generated
    )
    assert [m for m, _ in ai.calls].count("generate_misconceptions") == 2
    concepts = await ai_concepts(conn, world)
    assert (
        await conn.fetchval(
            "select count(*) from misconceptions where concept_id = $1", concepts[0]["id"]
        )
        == 1
    )
    assert await conn.fetchval("select status::text from jobs where id = $1", job_id) == "succeeded"


async def test_build_fails_when_a_concept_still_has_no_misconceptions(
    conn: asyncpg.Connection, world: World
) -> None:
    section_id, job_id, ai = await build_with_misconception_replies(
        conn, world, not_generated, not_generated, not_generated
    )
    assert [m for m, _ in ai.calls].count("generate_misconceptions") == 3
    assert (
        await conn.fetchval(
            "select build_status::text from material_sections where id = $1", section_id
        )
        == "failed"
    )
    assert await conn.fetchval("select error_code from jobs where id = $1", job_id) == (
        "MISCONCEPTIONS_INCOMPLETE"
    )
