from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel

from nalar.application.features.integrity.commands.compute_publication_similarity import (
    ComputePublicationSimilarityHandler,
)
from nalar.application.features.release.commands.finalize_publication import (
    FinalizePublicationHandler,
)
from nalar.application.features.release.commands.release_reminders import ReleaseRemindersHandler
from nalar.application.features.scheduler.commands.tick import SchedulerTiming, TickHandler
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import ClassInsightOut, ParentSummaryOut
from nalar.infrastructure.config import load_integrity_config, load_narrative_lexicon
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, evaluate, finish_session
from tests.integration.support.uow import uow_on
from tests.integration.test_release import close_runs
from tests.unit.application.fakes import (
    FakeClock,
    RecordingBackground,
    ScriptedAiGateway,
    invocation,
)

GOOD = "Ananda menjelaskan mengapa kelereng berhenti dengan kata-katanya sendiri."


async def settle(conn: asyncpg.Connection, world: World) -> None:
    await finish_session(conn, world, world.session_id)
    await evaluate(conn, world, world.session_id)
    await close_runs(conn, world)


async def finalize_job(conn: asyncpg.Connection, world: World) -> UUID:
    job_id: UUID = await conn.fetchval(
        "insert into jobs (school_id, kind, entity_type, entity_id)"
        " values ($1, 'publication_finalize', 'publications', $2) returning id",
        world.school_id,
        world.publication_id,
    )
    return job_id


def finalizer(conn: asyncpg.Connection, ai: ScriptedAiGateway) -> FinalizePublicationHandler:
    uow = uow_on(conn)
    return FinalizePublicationHandler(
        uow,
        ai,
        ComputePublicationSimilarityHandler(uow, load_integrity_config()),
        load_narrative_lexicon(),
    )


def summary(text: str, source: str = "model") -> AiResult[ParentSummaryOut]:
    return AiResult(
        ParentSummaryOut.model_validate({"content": text, "source": source}),
        [invocation("parent_summary")],
    )


def insight_reply(body: BaseModel) -> AiResult[ClassInsightOut]:
    concept = next(c for c in body.concepts if c.mastered_count == 1).concept_id  # type: ignore[attr-defined]
    return AiResult(
        ClassInsightOut.model_validate(
            {
                "narrative": f"Sebanyak {{{{mastered:{concept}}}}} dari {{{{total}}}} siswa"
                " menguasai konsep ini.",
                "clusters": [],
            }
        ),
        [invocation("class_map_insight")],
    )


async def test_finalizer_stores_summaries_and_a_filled_insight(
    conn: asyncpg.Connection, world: World
) -> None:
    await settle(conn, world)
    ai = ScriptedAiGateway()
    ai.script("parent_summary", summary(GOOD))
    ai.script("class_insight", insight_reply)
    job_id = await finalize_job(conn, world)
    await finalizer(conn, ai).execute(world.publication_id, job_id)
    row = await conn.fetchrow(
        "select content, ai_invocation_id from parent_summaries where publication_id = $1",
        world.publication_id,
    )
    assert row is not None
    assert row["content"] == GOOD and row["ai_invocation_id"] is not None
    request: Any = dict(ai.calls)["parent_summary"]
    sent = request.model_dump_json()
    assert str(world.student_id) not in sent
    assert "Pengguna Uji" not in sent
    async with api_client(conn) as api:
        body = (
            await api.get(
                f"/publications/{world.publication_id}/class-map",
                headers=as_user(world.teacher_id),
            )
        ).json()
    assert body["insight"]["narrative"] == "Sebanyak 1 dari 1 siswa menguasai konsep ini."
    assert await conn.fetchval("select status::text from jobs where id = $1", job_id) == "succeeded"


async def test_template_summary_is_stored_without_an_invocation_id(
    conn: asyncpg.Connection, world: World
) -> None:
    await settle(conn, world)
    ai = ScriptedAiGateway()
    ai.script("parent_summary", summary(GOOD, source="template"))
    ai.script("class_insight", AiServiceError("upstream_unavailable", 503))
    await finalizer(conn, ai).execute(world.publication_id, await finalize_job(conn, world))
    assert (
        await conn.fetchval(
            "select ai_invocation_id from parent_summaries where publication_id = $1",
            world.publication_id,
        )
        is None
    )


async def test_summary_with_a_number_is_not_stored(conn: asyncpg.Connection, world: World) -> None:
    await settle(conn, world)
    ai = ScriptedAiGateway()
    ai.script("parent_summary", summary("Ananda menjawab 3 dari 4 pertanyaan."))
    ai.script("class_insight", AiServiceError("upstream_unavailable", 503))
    await finalizer(conn, ai).execute(world.publication_id, await finalize_job(conn, world))
    assert (
        await conn.fetchval(
            "select count(*) from parent_summaries where publication_id = $1", world.publication_id
        )
        == 0
    )
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations"
            " where school_id = $1 and purpose = 'parent_summary'",
            world.school_id,
        )
        == 1
    )


async def test_s5_outage_leaves_summaries_pending_and_the_job_succeeds(
    conn: asyncpg.Connection, world: World
) -> None:
    await settle(conn, world)
    ai = ScriptedAiGateway()
    down = AiServiceError("upstream_unavailable", 503, [invocation("parent_summary", "error")])
    ai.script("parent_summary", down)
    ai.script("class_insight", AiServiceError("upstream_unavailable", 503))
    job_id = await finalize_job(conn, world)
    await finalizer(conn, ai).execute(world.publication_id, job_id)
    assert await conn.fetchval("select status::text from jobs where id = $1", job_id) == "succeeded"
    async with api_client(conn) as api:
        body = (
            await api.get(
                f"/publications/{world.publication_id}/release-preview",
                headers=as_user(world.teacher_id),
            )
        ).json()
    assert {"code": "SUMMARIES_PENDING", "count": 1} in body["blockers"]
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1 and status = 'failed'",
            world.school_id,
        )
        == 1
    )


async def test_summaries_are_frozen_after_release(conn: asyncpg.Connection, world: World) -> None:
    await settle(conn, world)
    await conn.execute(
        "update publications set released_to_parents_at = now(), released_by = $2 where id = $1",
        world.publication_id,
        world.teacher_id,
    )
    ai = ScriptedAiGateway()
    ai.script("class_insight", AiServiceError("upstream_unavailable", 503))
    await finalizer(conn, ai).execute(world.publication_id, await finalize_job(conn, world))
    assert "parent_summary" not in {m for m, _ in ai.calls}


async def test_a_finished_job_is_not_run_again(conn: asyncpg.Connection, world: World) -> None:
    await settle(conn, world)
    job_id = await finalize_job(conn, world)
    await conn.execute("update jobs set status = 'succeeded' where id = $1", job_id)
    ai = ScriptedAiGateway()
    await finalizer(conn, ai).execute(world.publication_id, job_id)
    assert ai.calls == []


async def test_tick_enqueues_the_finalizer_once_per_cooldown(
    conn: asyncpg.Connection, world: World
) -> None:
    await settle(conn, world)
    timing = SchedulerTiming(
        recovery_after=timedelta(seconds=15),
        evaluation_sweep_after=timedelta(minutes=2),
        finalize_cooldown=timedelta(minutes=10),
    )
    tick = TickHandler(uow_on(conn), FakeClock(), RecordingBackground(), timing)
    first = await tick.execute()
    await tick.execute()
    assert first.finalizing >= 1
    assert (
        await conn.fetchval(
            "select count(*) from jobs where kind = 'publication_finalize' and entity_id = $1",
            world.publication_id,
        )
        == 1
    )


async def test_release_reminder_is_sent_once(conn: asyncpg.Connection, world: World) -> None:
    await settle(conn, world)
    await conn.execute(
        "insert into parent_summaries (school_id, publication_id, student_id, content)"
        " values ($1, $2, $3, $4)",
        world.school_id,
        world.publication_id,
        world.student_id,
        GOOD,
    )
    await conn.execute(
        "insert into jobs (school_id, kind, entity_type, entity_id, status, updated_at)"
        " values ($1, 'publication_finalize', 'publications', $2, 'succeeded',"
        " now() - interval '2 days')",
        world.school_id,
        world.publication_id,
    )
    handler = ReleaseRemindersHandler(uow_on(conn), FakeClock())
    assert await handler.execute() == 1
    assert await handler.execute() == 0
