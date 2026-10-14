from uuid import UUID

import asyncpg

from nalar.application.features.results.commands.teaching_plan import BuildTeachingPlanHandler
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import TeachingPlanIn, TeachingPlanOut
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import ScriptedAiGateway, invocation

STEPS = [
    {"phase": p, "teacher_action": "Tunjukkan dua benda.", "ask": "Apa yang terjadi?"}
    for p in ("elicit", "contrast", "explain")
]


def plan_reply(body: TeachingPlanIn) -> AiResult[TeachingPlanOut]:
    return AiResult(
        TeachingPlanOut(
            misconception_id=body.misconception_id,
            objective="Siswa menjelaskan gaya gesek dengan alasan sendiri.",
            steps=STEPS,
            counter_example=None,
            check_question="Mengapa buku berhenti di lantai kasar?",
            look_for="Siswa menyebut gesekan, bukan gaya yang habis.",
            question_ids=[],
        ),
        [invocation("teaching_plan")],
    )


async def request(conn: asyncpg.Connection, world: World, user: UUID) -> tuple[int, dict]:  # type: ignore[type-arg]
    async with api_client(conn) as api:
        response = await api.post(
            f"/publications/{world.publication_id}/teaching-plans",
            json={"misconception_id": str(world.misconception_ids[0])},
            headers=as_user(user),
        )
    return response.status_code, response.json()


async def test_teacher_requests_one_plan_that_the_class_map_then_shows(
    conn: asyncpg.Connection, world: World
) -> None:
    assert (await request(conn, world, world.student_id))[0] == 404
    status, queued = await request(conn, world, world.teacher_id)
    assert status == 202
    assert (await request(conn, world, world.teacher_id))[1] == queued  # deduped while queued

    ai = ScriptedAiGateway()
    ai.script("teaching_plan", plan_reply)
    await BuildTeachingPlanHandler(uow_on(conn), ai).execute(
        UUID(queued["job_id"]), world.publication_id, world.misconception_ids[0]
    )
    sent = ai.calls[0][1].model_dump_json()
    name = await conn.fetchval("select full_name from profiles where id = $1", world.student_id)
    assert name not in sent and str(world.student_id) not in sent  # reviewed content only

    async with api_client(conn) as api:
        mapped = (
            await api.get(
                f"/publications/{world.publication_id}/class-map",
                headers=as_user(world.teacher_id),
            )
        ).json()
    plans = {
        m["misconception_id"]: m["teaching_plan"]
        for c in mapped["concepts"]
        for m in c["misconceptions"]
    }
    assert plans[str(world.misconception_ids[0])]["look_for"].startswith("Siswa")
    assert (await request(conn, world, world.teacher_id))[1] == {"job_id": None}


async def test_failed_draft_keeps_provenance_and_no_plan(
    conn: asyncpg.Connection, world: World
) -> None:
    _, queued = await request(conn, world, world.teacher_id)
    ai = ScriptedAiGateway()
    ai.script(
        "teaching_plan", AiServiceError("ai_output_invalid", 502, [invocation("teaching_plan")])
    )
    await BuildTeachingPlanHandler(uow_on(conn), ai).execute(
        UUID(queued["job_id"]), world.publication_id, world.misconception_ids[0]
    )
    row = await conn.fetchrow(
        "select status::text, error_code from jobs where id = $1", UUID(queued["job_id"])
    )
    assert row is not None and tuple(row) == ("failed", "ai_output_invalid")
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations"
            " where school_id = $1 and purpose = 'teaching_plan'",
            world.school_id,
        )
        == 1
    )
    assert not await conn.fetchval("select exists (select 1 from teaching_plans)")
