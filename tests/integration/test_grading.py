import asyncpg

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, add_flag, add_score, create_teacher


async def test_override_keeps_ai_level_and_writes_audit(
    conn: asyncpg.Connection, world: World
) -> None:
    score_id = await add_score(conn, world, world.session_id)
    async with api_client(conn) as api:
        response = await api.post(
            f"/scores/{score_id}/overrides",
            json={"final_level": 4, "reason": "Mekanisme dijelaskan lisan di kelas."},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 200
    body = response.json()
    assert (body["ai_level"], body["final_level"]) == (2, 4)
    row = await conn.fetchrow(
        "select ai_level, final_level from evaluation_scores where id = $1", score_id
    )
    assert row is not None
    assert (row["ai_level"], row["final_level"]) == (2, 4)
    assert (
        await conn.fetchval(
            "select previous_level from score_overrides where score_id = $1", score_id
        )
        == 2
    )
    assert (
        await conn.fetchval(
            "select count(*) from audit_logs where action = 'score.overridden' and entity_id = $1",
            score_id,
        )
        == 1
    )


async def test_blank_override_reason_returns_400(conn: asyncpg.Connection, world: World) -> None:
    score_id = await add_score(conn, world, world.session_id)
    async with api_client(conn) as api:
        response = await api.post(
            f"/scores/{score_id}/overrides",
            json={"final_level": 3, "reason": "   "},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 400


async def test_override_to_the_same_level_returns_409(
    conn: asyncpg.Connection, world: World
) -> None:
    score_id = await add_score(conn, world, world.session_id)
    async with api_client(conn) as api:
        response = await api.post(
            f"/scores/{score_id}/overrides",
            json={"final_level": 2, "reason": "Sama saja."},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCORE_UNCHANGED"


async def test_unassigned_teacher_in_same_school_gets_404(
    conn: asyncpg.Connection, world: World
) -> None:
    score_id = await add_score(conn, world, world.session_id)
    flag_id = await add_flag(conn, world, world.session_id)
    colleague = await create_teacher(conn, world.school_id)
    async with api_client(conn) as api:
        override = await api.post(
            f"/scores/{score_id}/overrides",
            json={"final_level": 3, "reason": "x"},
            headers=as_user(colleague),
        )
        review = await api.post(
            f"/flags/{flag_id}/review", json={"decision": "cleared"}, headers=as_user(colleague)
        )
    assert (override.status_code, review.status_code) == (404, 404)


async def test_flag_review_is_idempotent_and_a_different_decision_conflicts(
    conn: asyncpg.Connection, world: World
) -> None:
    flag_id = await add_flag(conn, world, world.session_id)
    headers = as_user(world.teacher_id)
    async with api_client(conn) as api:
        first = await api.post(
            f"/flags/{flag_id}/review",
            json={"decision": "cleared", "note": "Siswa mengetik ulang catatan sendiri."},
            headers=headers,
        )
        again = await api.post(
            f"/flags/{flag_id}/review", json={"decision": "cleared"}, headers=headers
        )
        other = await api.post(
            f"/flags/{flag_id}/review", json={"decision": "concern_confirmed"}, headers=headers
        )
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "FLAG_ALREADY_REVIEWED"
    assert (
        await conn.fetchval(
            "select count(*) from audit_logs where action = 'flag.reviewed' and entity_id = $1",
            flag_id,
        )
        == 1
    )
