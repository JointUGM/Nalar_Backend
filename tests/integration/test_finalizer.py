import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
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
from nalar.application.ports.ai_contract import ClassInsightIn, ClassInsightOut, ParentSummaryOut
from nalar.infrastructure.config import load_integrity_config, load_narrative_lexicon
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.release import PgReleaseRepo
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world, evaluate, finish_session
from tests.integration.support.uow import uow_on
from tests.integration.test_release import close_runs
from tests.unit.application.fakes import (
    FakeClock,
    RecordingBackground,
    ScriptedAiGateway,
    invocation,
)

GOOD = "Ananda membandingkan dua permukaan dengan kata-katanya sendiri."


async def settle(conn: DbConnection, world: World) -> None:
    await finish_session(conn, world, world.session_id)
    await evaluate(conn, world, world.session_id)
    await close_runs(conn, world)


async def finalize_job(conn: DbConnection, world: World) -> UUID:
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
                "suggestions": [
                    "Minta siswa menjelaskan prediksi tentang gaya gesek setelah pengamatan.",
                    "Ajak {{total}} siswa membandingkan alasan tentang gaya gesek.",
                ],
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
        again = await api.get(
            f"/publications/{world.publication_id}/class-map",
            headers=as_user(world.teacher_id),
        )
        assert again.json() == body
        for actor in (world.student_id, world.parent_id):
            denied = await api.get(
                f"/publications/{world.publication_id}/class-map",
                headers=as_user(actor),
            )
            assert denied.status_code == 404
    assert body["insight"]["narrative"] == "Sebanyak 1 dari 1 siswa menguasai konsep ini."
    assert body["insight"]["suggestions"] == [
        "Minta siswa menjelaskan prediksi tentang gaya gesek setelah pengamatan.",
        "Ajak 1 siswa membandingkan alasan tentang gaya gesek.",
    ]
    stored = await conn.fetch(
        "select s.school_id, s.rank, s.content from follow_up_suggestions s"
        " join class_map_insights i on i.id = s.insight_id where i.publication_id = $1"
        " order by s.rank",
        world.publication_id,
    )
    assert [r["rank"] for r in stored] == [1, 2]
    assert all(r["school_id"] == world.school_id for r in stored)
    assert stored[1]["content"] == "Ajak {{total}} siswa membandingkan alasan tentang gaya gesek."
    await finalizer(conn, ai).execute(world.publication_id, await finalize_job(conn, world))
    assert [m for m, _ in ai.calls].count("class_insight") == 1
    assert await conn.fetchval("select status::text from jobs where id = $1", job_id) == "succeeded"


@pytest.mark.parametrize(
    "suggestion",
    [
        "Ajak 12 siswa berdiskusi.",
        "Ajak {{count:00000000-0000-4000-8000-000000000099}} siswa.",
    ],
)
async def test_invalid_suggestion_stores_provenance_without_any_insight(
    conn: asyncpg.Connection, world: World, suggestion: str
) -> None:
    await settle(conn, world)
    ai = ScriptedAiGateway()
    ai.script("parent_summary", summary(GOOD))

    def invalid(body: BaseModel) -> AiResult[ClassInsightOut]:
        reply = insight_reply(body)
        reply.result.suggestions = [suggestion]
        return reply

    ai.script("class_insight", invalid)
    await finalizer(conn, ai).execute(world.publication_id, await finalize_job(conn, world))
    assert not await PgReleaseRepo(conn).has_insight(world.publication_id)
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1"
            " and purpose = 'class_map_insight'",
            world.school_id,
        )
        == 1
    )
    assert (
        await conn.fetchval(
            "select count(*) from follow_up_suggestions where school_id = $1",
            world.school_id,
        )
        == 0
    )


async def test_empty_suggestions_are_stored_once_and_legacy_insights_stay_unchanged(
    conn: asyncpg.Connection, world: World
) -> None:
    await settle(conn, world)
    async with uow_on(conn) as uow:
        await uow.release.insert_insight(
            world.school_id,
            world.publication_id,
            {"denominator": 1, "incomplete_count": 0, "concepts": []},
            [],
            "Diskusikan gaya gesek.",
            None,
            [],
        )
    ai = ScriptedAiGateway()
    ai.script("parent_summary", summary(GOOD))
    await finalizer(conn, ai).execute(world.publication_id, await finalize_job(conn, world))
    assert "class_insight" not in {m for m, _ in ai.calls}
    async with api_client(conn) as api:
        reply = await api.get(
            f"/publications/{world.publication_id}/class-map",
            headers=as_user(world.teacher_id),
        )
    assert reply.json()["insight"]["suggestions"] == []


async def test_suggestion_write_failure_rolls_back_the_insight(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute("""
        create function pg_temp.reject_test_suggestion() returns trigger language plpgsql as $$
        begin raise exception 'injected suggestion write failure'; end $$;
        create trigger reject_test_suggestion before insert on follow_up_suggestions
        for each row execute function pg_temp.reject_test_suggestion();
    """)
    with pytest.raises(asyncpg.RaiseError, match="injected suggestion write failure"):
        async with uow_on(conn) as uow:
            await uow.release.insert_insight(
                world.school_id,
                world.publication_id,
                {},
                [],
                "Diskusikan gaya.",
                None,
                ["Diskusikan gaya gesek."],
            )
    assert not await PgReleaseRepo(conn).has_insight(world.publication_id)


async def test_competing_finalizers_store_one_insight_and_one_ranked_suggestion_set(
    pool: asyncpg.Pool,
    eval_queue_guard: None,
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        world = await build_world(conn)
        await settle(conn, world)
        jobs = [await finalize_job(conn, world), await finalize_job(conn, world)]
    ready = asyncio.Event()

    class CompetingAi(ScriptedAiGateway):
        async def class_insight(
            self, body: ClassInsightIn, request_id: str
        ) -> AiResult[ClassInsightOut]:
            reply = await super().class_insight(body, request_id)
            if sum(m == "class_insight" for m, _ in self.calls) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            return reply

    ai = CompetingAi()
    ai.script("parent_summary", summary(GOOD), summary(GOOD))
    ai.script("class_insight", insight_reply, insight_reply)

    async def run(job: UUID) -> None:
        uow = PgUnitOfWork(pool.acquire)
        await FinalizePublicationHandler(
            uow,
            ai,
            ComputePublicationSimilarityHandler(uow, load_integrity_config()),
            load_narrative_lexicon(),
        ).execute(world.publication_id, job)

    await asyncio.wait_for(asyncio.gather(*(run(job) for job in jobs)), 15)
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "select count(*) from class_map_insights where publication_id = $1",
                world.publication_id,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "select count(*) from follow_up_suggestions where school_id = $1",
                world.school_id,
            )
            == 2
        )
        assert (
            await conn.fetchval(
                "select count(*) from ai_invocations where school_id = $1"
                " and purpose = 'class_map_insight'",
                world.school_id,
            )
            == 2
        )


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
