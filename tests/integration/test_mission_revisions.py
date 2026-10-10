from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from nalar.application.features.missions.commands.build_revision import BuildRevisionHandler
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import ReviseMissionOut
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World
from tests.integration.support.uow import uow_on
from tests.integration.test_mission_generation import generated
from tests.unit.application.fakes import FakeClock, ScriptedAiGateway, invocation


def intent(world: World, **overrides: Any) -> dict[str, Any]:
    return {
        "base_version_id": str(world.version_id),
        "expected_latest_version_id": str(world.version_id),
        "feedback": [],
        "learning_objective": "Menjelaskan gerak melalui bukti pengamatan",
        **overrides,
    }


async def submit(
    conn: asyncpg.Connection,
    world: World,
    body: dict[str, Any] | None = None,
    key: UUID | None = None,
) -> httpx.Response:
    async with api_client(conn, overrides={"mission_revision_enabled": True}) as api:
        return await api.post(
            f"/missions/{world.mission_id}/revise",
            json=body or intent(world),
            headers={**as_user(world.teacher_id), "Idempotency-Key": str(key or uuid4())},
        )


async def test_revision_snapshots_goal_and_keeps_published_version_immutable(
    conn: asyncpg.Connection, world: World
) -> None:
    before_row = await conn.fetchrow("select * from mission_versions where id=$1", world.version_id)
    assert before_row is not None
    before = dict(before_row)
    response = await submit(conn, world)
    assert response.status_code == 202, response.text
    job_id = UUID(response.json()["job_id"])
    ai = ScriptedAiGateway()
    ai.script(
        "revise_mission",
        AiResult(
            ReviseMissionOut(
                generation=generated(world).result,
                effective_scope=["anchor_problem", "reference_reasoning", "rubric", "bank"],
                changed_fields=[],
            ),
            [invocation("mission_generation")],
        ),
    )
    handler = BuildRevisionHandler(uow_on(conn), ai, FakeClock())
    await handler.execute(world.mission_id, job_id)
    await handler.execute(world.mission_id, job_id)
    job = await conn.fetchrow("select status,error_code from jobs where id=$1", job_id)
    assert job is not None
    assert job["status"] == "succeeded", dict(job)
    after_row = await conn.fetchrow("select * from mission_versions where id=$1", world.version_id)
    assert after_row is not None
    assert dict(after_row) == before
    assert (
        await conn.fetchval(
            "select count(*) from mission_versions where generation_job_id=$1", job_id
        )
        == 1
    )
    async with api_client(conn, overrides={"mission_revision_enabled": True}) as api:
        draft = (
            await api.get(
                f"/missions/{world.mission_id}/versions/2", headers=as_user(world.teacher_id)
            )
        ).json()
        assert draft["learning_objective"] == intent(world)["learning_objective"]
        assert draft["base_version_id"] == str(world.version_id)
        assert draft["base_version_number"] == 1
        assert draft["status"] == "draft"
        old = (
            await api.get(
                f"/missions/{world.mission_id}/versions/1", headers=as_user(world.teacher_id)
            )
        ).json()
        assert old["learning_objective"] != draft["learning_objective"]


async def test_revision_replays_key_and_rejects_different_active_intent(
    conn: asyncpg.Connection, world: World
) -> None:
    key = uuid4()
    first = await submit(conn, world, key=key)
    again = await submit(conn, world, key=key)
    assert first.status_code == 202, first.text
    assert first.json()["job_id"] == again.json()["job_id"]
    reused = await submit(conn, world, intent(world, title="Judul lain"), key)
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    other = await submit(conn, world, intent(world, title="Judul lain"))
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "REVISION_IN_PROGRESS"


async def test_title_only_revision_spends_nothing_and_revalidates_latest(
    conn: asyncpg.Connection, world: World
) -> None:
    queued = await submit(conn, world, intent(world, learning_objective=None))
    assert queued.status_code == 400
    body = intent(world, title="Judul versi baru")
    del body["learning_objective"]
    queued = await submit(conn, world, body)
    assert queued.status_code == 202, queued.text
    ai = ScriptedAiGateway()
    await BuildRevisionHandler(uow_on(conn), ai, FakeClock()).execute(
        world.mission_id, UUID(queued.json()["job_id"])
    )
    assert ai.calls == []
    assert (
        await conn.fetchval(
            "select title_snapshot from mission_versions where mission_id=$1 and version_number=2",
            world.mission_id,
        )
        == body["title"]
    )
    stale = await submit(conn, world)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "MISSION_VERSION_CHANGED"


async def test_failed_revision_records_paid_invocations_without_a_draft(
    conn: asyncpg.Connection, world: World
) -> None:
    queued = await submit(conn, world)
    assert queued.status_code == 202, queued.text
    job_id = UUID(queued.json()["job_id"])
    ai = ScriptedAiGateway()
    paid = invocation("mission_generation")
    ai.script("revise_mission", AiServiceError("ai_output_invalid", 502, [paid]))
    await BuildRevisionHandler(uow_on(conn), ai, FakeClock()).execute(world.mission_id, job_id)
    assert await conn.fetchval("select status from jobs where id=$1", job_id) == "failed"
    assert (
        await conn.fetchval(
            "select count(*) from mission_versions where generation_job_id=$1", job_id
        )
        == 0
    )
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where request_id=$1 and school_id=$2",
            paid.request_id,
            world.school_id,
        )
        == 1
    )


async def test_revision_is_owner_only_and_request_storage_is_private(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn, overrides={"mission_revision_enabled": True}) as api:
        reply = await api.post(
            f"/missions/{world.mission_id}/revise",
            json=intent(world),
            headers={**as_user(world.student_id), "Idempotency-Key": str(uuid4())},
        )
    assert reply.status_code in (403, 404)
    assert (
        await conn.fetchval(
            "select has_table_privilege('authenticated','mission_revision_requests','SELECT')"
        )
        is False
    )
    with pytest.raises(asyncpg.RaiseError):
        async with conn.transaction():
            await conn.execute(
                "update mission_versions set learning_objective_snapshot='new' where id=$1",
                world.version_id,
            )


async def test_failed_revision_request_is_recoverable_only_by_its_owner(
    conn: asyncpg.Connection, world: World
) -> None:
    queued = await submit(conn, world)
    assert queued.status_code == 202, queued.text
    job_id = UUID(queued.json()["job_id"])
    ai = ScriptedAiGateway()
    ai.script("revise_mission", AiServiceError("ai_output_invalid", 502, []))
    await BuildRevisionHandler(uow_on(conn), ai, FakeClock()).execute(world.mission_id, job_id)
    async with api_client(conn) as api:
        url = f"/missions/{world.mission_id}/revisions/{job_id}"
        own = await api.get(url, headers=as_user(world.teacher_id))
        assert own.status_code == 200, own.text
        assert own.json()["status"] == "failed"
        assert own.json()["intent"]["learning_objective"] == intent(world)["learning_objective"]
        assert "ai_input" not in own.text and "base_draft" not in own.text
        wrong = await api.get(
            f"/missions/{uuid4()}/revisions/{job_id}", headers=as_user(world.teacher_id)
        )
        assert wrong.status_code == 404
        student = await api.get(url, headers=as_user(world.student_id))
        assert student.status_code == 404


async def test_upstream_cannot_change_session_limits_during_revision(
    conn: asyncpg.Connection, world: World
) -> None:
    queued = await submit(conn, world)
    assert queued.status_code == 202, queued.text
    job_id = UUID(queued.json()["job_id"])
    output = generated(world).result
    output.context_pack.max_probes = 5
    ai = ScriptedAiGateway()
    ai.script(
        "revise_mission",
        AiResult(
            ReviseMissionOut(
                generation=output,
                effective_scope=["anchor_problem", "reference_reasoning", "rubric", "bank"],
                changed_fields=[],
            ),
            [],
        ),
    )
    await BuildRevisionHandler(uow_on(conn), ai, FakeClock()).execute(world.mission_id, job_id)
    assert (
        await conn.fetchval("select error_code from jobs where id=$1", job_id)
        == "REVISION_SETTINGS_CHANGED"
    )
    assert (
        await conn.fetchval(
            "select count(*) from mission_versions where generation_job_id=$1", job_id
        )
        == 0
    )
