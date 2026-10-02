import json
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import asyncpg
import pytest

from nalar.application.features.missions.commands.generate_mission import BuildMissionHandler
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import (
    GenerateMissionIn,
    GenerateMissionOut,
    SelectTargetsOut,
)
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import RUBRIC, World, create_concept
from tests.integration.support.uow import uow_on
from tests.integration.test_missions import new_mission
from tests.unit.application.fakes import FakeClock, ScriptedAiGateway, invocation


class HookAi(ScriptedAiGateway):
    hook: Callable[[], Awaitable[None]] | None = None

    async def generate_mission(
        self, body: GenerateMissionIn, request_id: str
    ) -> AiResult[GenerateMissionOut]:
        reply = await super().generate_mission(body, request_id)
        if self.hook is not None:
            await self.hook()
        return reply


def generated(world: World, sources: list[UUID] | None = None) -> AiResult[GenerateMissionOut]:
    pack = dict(world.pack)
    pack["max_probes"] = 6
    pack["max_duration_minutes"] = 20
    bank = [dict(q) for q in pack["question_bank"]]
    pack["question_bank"] = [
        *bank,
        *[
            {**q, "id": q["id"] + "-second", "text": "Dalam keadaan lain, " + q["text"]}
            for q in bank
        ],
    ]
    output = GenerateMissionOut.model_validate(
        {
            "context_pack": pack,
            "rubric": RUBRIC,
            "source_chunk_ids": sources or [],
            "ungrounded_concept_ids": [] if sources else list(world.concept_ids),
            "probe_plan": {},
        }
    )
    return AiResult(output, [invocation("mission_generation"), invocation("mission_critic")])


async def queued(conn: asyncpg.Connection, world: World) -> tuple[UUID, UUID]:
    async with api_client(conn) as api:
        mission = await new_mission(api, world)
        response = await api.post(
            f"/missions/{mission}/generate", json={}, headers=as_user(world.teacher_id)
        )
    assert response.status_code == 202, response.text
    return UUID(mission), UUID(response.json()["job_id"])


def script_success(ai: ScriptedAiGateway, world: World, sources: list[UUID] | None = None) -> None:
    ai.script(
        "select_targets",
        AiResult(
            SelectTargetsOut(target_concept_ids=list(world.concept_ids)),
            [invocation("mission_generation")],
        ),
    )
    ai.script("generate_mission", generated(world, sources))


async def test_generation_creates_one_unreviewed_draft_and_job_exposes_only_metadata(
    conn: asyncpg.Connection, world: World
) -> None:
    mission, job = await queued(conn, world)
    ai = ScriptedAiGateway()
    script_success(ai, world)
    handler = BuildMissionHandler(uow_on(conn), ai, FakeClock())
    await handler.execute(mission, job)
    await handler.execute(mission, job)
    assert len(ai.calls) == 2
    row = await conn.fetchrow("select * from mission_versions where generation_job_id = $1", job)
    assert row is not None and row["reviewed_at"] is None and row["locked_at"] is None
    assert row["context_pack"] is None
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1", world.school_id
        )
        == 3
    )
    async with api_client(conn) as api:
        status = await api.get(f"/jobs/{job}", headers=as_user(world.teacher_id))
        forbidden = await api.get(f"/jobs/{job}", headers=as_user(world.student_id))
    assert status.status_code == 200 and forbidden.status_code == 404
    result = status.json()["generation_result"]
    assert result["version_id"] == str(row["id"])
    assert result["ungrounded_concept_ids"] == [str(c) for c in world.concept_ids]
    assert "reference_reasoning" not in json.dumps(status.json())


async def test_pending_items_are_excluded_and_unlinked_sources_are_not_retrieved(
    conn: asyncpg.Connection, world: World
) -> None:
    pending = await create_concept(conn, world.school_id, world.kb_id, review_status="pending")
    mission, job = await queued(conn, world)
    ai = ScriptedAiGateway()
    script_success(ai, world)
    await BuildMissionHandler(uow_on(conn), ai, FakeClock()).execute(mission, job)
    selected = ai.calls[0][1].model_dump(mode="json")
    assert str(pending) not in json.dumps(selected)
    request = ai.calls[1][1].model_dump(mode="json")
    assert request["paragraphs"] == []


async def test_transient_generation_failure_preserves_selection_checkpoint_and_paid_attempt(
    conn: asyncpg.Connection, world: World
) -> None:
    mission, job = await queued(conn, world)
    ai = ScriptedAiGateway()
    script_success(ai, world)
    success = ai.replies["generate_mission"].pop()
    ai.script(
        "generate_mission",
        AiServiceError("upstream_unavailable", 503, [invocation("mission_generation", "error")]),
        success,
    )
    handler = BuildMissionHandler(uow_on(conn), ai, FakeClock())
    with pytest.raises(AiServiceError):
        await handler.execute(mission, job)
    assert await conn.fetchval("select status::text from jobs where id = $1", job) == "queued"
    await handler.execute(mission, job)
    assert [m for m, _ in ai.calls] == ["select_targets", "generate_mission", "generate_mission"]
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1", world.school_id
        )
        == 4
    )


async def test_revoked_permission_after_ai_call_keeps_provenance_but_saves_no_draft(
    conn: asyncpg.Connection, world: World
) -> None:
    mission, job = await queued(conn, world)
    ai = HookAi()
    script_success(ai, world)

    async def revoke() -> None:
        await conn.execute(
            "update school_memberships set status = 'inactive'"
            " where school_id = $1 and user_id = $2",
            world.school_id,
            world.teacher_id,
        )

    ai.hook = revoke
    await BuildMissionHandler(uow_on(conn), ai, FakeClock()).execute(mission, job)
    assert await conn.fetchval("select status::text from jobs where id = $1", job) == "failed"
    assert (
        await conn.fetchval(
            "select count(*) from mission_versions where generation_job_id = $1", job
        )
        == 0
    )
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1", world.school_id
        )
        == 3
    )


async def test_fresh_running_lease_skips_duplicate_model_calls(
    conn: asyncpg.Connection, world: World
) -> None:
    mission, job = await queued(conn, world)
    await conn.execute("update jobs set status = 'running' where id = $1", job)
    ai = ScriptedAiGateway()
    await BuildMissionHandler(uow_on(conn), ai, FakeClock()).execute(mission, job)
    assert ai.calls == []


async def test_unknown_target_from_ai_is_terminal_and_records_its_cost(
    conn: asyncpg.Connection, world: World
) -> None:
    mission, job = await queued(conn, world)
    ai = ScriptedAiGateway()
    ai.script(
        "select_targets",
        AiResult(
            SelectTargetsOut(target_concept_ids=[world.concept_ids[0], uuid4()]),
            [invocation("mission_generation")],
        ),
    )
    await BuildMissionHandler(uow_on(conn), ai, FakeClock()).execute(mission, job)
    assert (
        await conn.fetchval("select error_code from jobs where id = $1", job)
        == "MISSION_TARGET_SELECTION_INVALID"
    )
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1", world.school_id
        )
        == 1
    )


async def test_link_retrieval_is_scoped_to_school_kb_and_active_materials(
    conn: asyncpg.Connection, world: World
) -> None:
    from nalar.infrastructure.db.repositories.missions import PgMissionsRepo

    mission, _ = await queued(conn, world)
    repo = PgMissionsRepo(conn)
    reference = await repo.mission_ref(mission)
    assert reference is not None
    material = await conn.fetchval(
        "insert into teaching_materials"
        " (school_id,knowledge_base_id,uploaded_by,title,storage_path,mime_type)"
        " values ($1,$2,$3,'Sumber',$4,'application/pdf') returning id",
        world.school_id,
        world.kb_id,
        world.teacher_id,
        str(uuid4()),
    )
    chunk = await conn.fetchval(
        "insert into material_chunks"
        " (school_id,knowledge_base_id,material_id,chunk_index,content,chunk_kind)"
        " values ($1,$2,$3,0,'Isi contoh','example') returning id",
        world.school_id,
        world.kb_id,
        material,
    )
    rows = await repo.generation_paragraphs(reference, [chunk, uuid4()])
    assert [r.id for r in rows] == [chunk]
    await conn.execute("update teaching_materials set archived_at = now() where id = $1", material)
    assert await repo.generation_paragraphs(reference, [chunk]) == []


async def test_transient_failures_stop_after_three_attempts_and_allow_new_job(
    conn: asyncpg.Connection, world: World
) -> None:
    mission, job = await queued(conn, world)
    ai = ScriptedAiGateway()
    ai.script(
        "select_targets",
        *[
            AiServiceError("upstream_unavailable", 503, [invocation("mission_generation", "error")])
            for _ in range(3)
        ],
    )
    handler = BuildMissionHandler(uow_on(conn), ai, FakeClock())
    for _ in range(2):
        with pytest.raises(AiServiceError):
            await handler.execute(mission, job)
    await handler.execute(mission, job)
    await handler.execute(mission, job)
    assert len(ai.calls) == 3
    assert await conn.fetchval("select status::text from jobs where id = $1", job) == "failed"
    assert (
        await conn.fetchval(
            "select count(*) from mission_versions where generation_job_id = $1", job
        )
        == 0
    )
    async with api_client(conn) as api:
        response = await api.post(
            f"/missions/{mission}/generate", json={}, headers=as_user(world.teacher_id)
        )
    assert response.status_code == 202
    assert response.json()["job_id"] != str(job)


async def test_changed_catalog_invalidates_retry_checkpoint_before_new_ai_call(
    conn: asyncpg.Connection, world: World
) -> None:
    mission, job = await queued(conn, world)
    ai = ScriptedAiGateway()
    script_success(ai, world)
    ai.replies["generate_mission"].clear()
    ai.script("generate_mission", AiServiceError("timeout", 503, []))
    handler = BuildMissionHandler(uow_on(conn), ai, FakeClock())
    with pytest.raises(AiServiceError):
        await handler.execute(mission, job)
    await conn.execute(
        "update concepts set description = 'Isi konsep berubah' where id = $1", world.concept_ids[0]
    )
    await handler.execute(mission, job)
    assert len(ai.calls) == 2
    assert await conn.fetchval("select status::text from jobs where id = $1", job) == "failed"
    assert (
        await conn.fetchval("select error_code from jobs where id = $1", job)
        == "MISSION_ITEMS_CHANGED"
    )
