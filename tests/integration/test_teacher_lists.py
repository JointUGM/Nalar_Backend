from dataclasses import replace
from datetime import UTC, datetime

import asyncpg
import pytest

from nalar.infrastructure.db.repositories.sessions import PgSessionsRepo
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    add_flag,
    add_membership,
    assign_teacher,
    build_world,
    create_class,
    create_concept,
    create_misconception,
    create_mission_version,
    create_run,
    create_session,
    create_student,
    create_teacher,
    publish,
)
from tests.integration.test_release import settled, student_session, summarize


async def test_roster_counts_latest_attempt_without_child_join_fanout(
    conn: asyncpg.Connection, world: World
) -> None:
    never = await create_student(conn, world.school_id, world.class_id, world.year_id)
    await settled(conn, world)
    await conn.execute(
        "insert into session_concept_results (school_id, session_id, concept_id, outcome)"
        " values ($1, $2, $3, 'developing')",
        world.school_id,
        world.session_id,
        world.concept_ids[1],
    )
    await add_flag(conn, world, world.session_id)
    await add_flag(conn, world, world.session_id)
    headers = as_user(world.teacher_id)
    url = f"/teacher/classes/{world.class_id}/students"
    async with api_client(conn) as api:
        plain = await api.get(url, headers=headers)
        assert plain.status_code == 200, plain.text
        assert plain.json()["publication_id"] is None
        assert all(i["session_id"] is None and i["status"] is None for i in plain.json()["items"])
        selected = await api.get(
            url, params={"publication_id": str(world.publication_id)}, headers=headers
        )
        assert selected.status_code == 200, selected.text
        by_student = {i["student_id"]: i for i in selected.json()["items"]}
        assert by_student[str(never)]["status"] == "not_started"
        mine = by_student[str(world.student_id)]
        assert mine["concept_counts"] == {"mastered": 1, "developing": 1, "misconception": 0}
        assert mine["open_flag_count"] == 2
        assert mine["completed_at"] is not None
        latest = await create_session(
            conn, world.school_id, world.publication_id, world.run_id, world.student_id, 2
        )
        await PgSessionsRepo(conn).pause_for_safety(latest)
        current = await api.get(
            url, params={"publication_id": str(world.publication_id)}, headers=headers
        )
        mine = next(i for i in current.json()["items"] if i["student_id"] == str(world.student_id))
        assert (mine["session_id"], mine["status"], mine["completed_at"]) == (
            str(latest),
            "paused_safety",
            None,
        )
        assert mine["concept_counts"] == {"mastered": 0, "developing": 0, "misconception": 0}
        assert mine["open_flag_count"] == 0
        await conn.execute(
            "update sessions set status = 'completed', ended_at = now(),"
            " end_reason = 'student_completed' where id = $1",
            latest,
        )
        await conn.execute(
            "insert into session_concept_results (school_id, session_id, concept_id, outcome)"
            " values ($1, $2, $3, 'mastered')",
            world.school_id,
            latest,
            world.concept_ids[0],
        )
        pending = await api.get(
            url, params={"publication_id": str(world.publication_id)}, headers=headers
        )
        mine = next(i for i in pending.json()["items"] if i["student_id"] == str(world.student_id))
        assert mine["evaluation_status"] == "pending"
        assert mine["concept_counts"]["mastered"] == 0
        await conn.execute(
            "insert into session_evaluations (school_id, session_id, status)"
            " values ($1, $2, 'failed')",
            world.school_id,
            latest,
        )
        failed = await api.get(
            url, params={"publication_id": str(world.publication_id)}, headers=headers
        )
        mine = next(i for i in failed.json()["items"] if i["student_id"] == str(world.student_id))
        assert mine["evaluation_status"] == "failed"
        assert mine["concept_counts"]["mastered"] == 0


async def test_roster_rejects_unassigned_class_and_mismatched_publication(
    conn: asyncpg.Connection, world: World
) -> None:
    other_class = await create_class(conn, world.school_id, world.year_id, "8B")
    await assign_teacher(conn, world.school_id, other_class, world.subject_id, world.teacher_id)
    other = await build_world(conn)
    url = f"/teacher/classes/{world.class_id}/students"
    async with api_client(conn) as api:
        mismatch = await api.get(
            f"/teacher/classes/{other_class}/students",
            params={"publication_id": str(world.publication_id)},
            headers=as_user(world.teacher_id),
        )
        assert mismatch.status_code == 404
        for actor in (world.student_id, other.teacher_id):
            response = await api.get(url, headers=as_user(actor))
            assert response.status_code == 404
        foreign = await api.get(
            url,
            params={"publication_id": str(other.publication_id)},
            headers=as_user(world.teacher_id),
        )
        assert foreign.status_code == 404


async def test_assignments_list_the_callers_active_assignments(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        response = await api.get("/teacher/assignments", headers=as_user(world.teacher_id))
    items = response.json()["items"]
    assert [(i["class_id"], i["school_subject_id"]) for i in items] == [
        (str(world.class_id), str(world.subject_id))
    ]


async def test_publications_list_counts_for_assigned_classes_only(
    conn: asyncpg.Connection, world: World
) -> None:
    colleague = await create_teacher(conn, world.school_id)
    async with api_client(conn) as api:
        mine = await api.get("/teacher/publications", headers=as_user(world.teacher_id))
        theirs = await api.get("/teacher/publications", headers=as_user(colleague))
    [item] = mine.json()["items"]
    assert item["id"] == str(world.publication_id)
    assert item["counts"] == {"started": 1, "completed": 0, "timed_out": 0, "evaluated": 0}
    assert theirs.json()["items"] == []


async def test_filtering_by_an_unassigned_class_is_404(
    conn: asyncpg.Connection, world: World
) -> None:
    other_class = await create_class(conn, world.school_id, world.year_id, "8B")
    async with api_client(conn) as api:
        response = await api.get(
            "/teacher/publications",
            params={"class_id": str(other_class)},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 404


async def test_attention_pages_keep_full_counts_and_remove_resolved_items(
    conn: asyncpg.Connection, world: World
) -> None:
    sessions = PgSessionsRepo(conn)
    assert await sessions.pause_for_safety(world.session_id)
    paused_at = await conn.fetchval(
        "select safety_paused_at from sessions where id = $1", world.session_id
    )
    assert paused_at is not None
    assert not await sessions.pause_for_safety(world.session_id)
    assert (
        await conn.fetchval("select safety_paused_at from sessions where id = $1", world.session_id)
        == paused_at
    )
    flags = [await add_flag(conn, world, world.session_id) for _ in range(2)]
    concepts = [
        await create_concept(conn, world.school_id, world.kb_id, "pending") for _ in range(2)
    ]
    misconception = await create_misconception(conn, world.school_id, world.kb_id, concepts[0])
    _, version = await create_mission_version(conn, world.school_id, world.kb_id, world.teacher_id)
    publication = await publish(conn, world.school_id, version, world.class_id, world.teacher_id)
    run = await create_run(conn, world.school_id, publication)
    session = await create_session(conn, world.school_id, publication, run, world.student_id)
    ready_world = replace(world, publication_id=publication, run_id=run, session_id=session)
    await settled(conn, ready_world, summary=False)
    headers = as_user(world.teacher_id)
    params = {"school_id": str(world.school_id), "limit": "2"}
    async with api_client(conn) as api:
        missing = await api.get("/teacher/attention", params=params, headers=headers)
        assert missing.status_code == 200, missing.text
        assert missing.json()["counts"]["release_ready"] == 0
        await summarize(conn, ready_world, world.student_id)
        colleague = await create_teacher(conn, world.school_id)
        theirs = await api.get("/teacher/attention", params=params, headers=as_user(colleague))
        assert theirs.json()["counts"]["total"] == 0
        await conn.execute(
            "update sessions set safety_paused_at = $2 where id = $1",
            world.session_id,
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        items = []
        cursor = None
        while True:
            page = await api.get(
                "/teacher/attention",
                params=params | ({"cursor": cursor} if cursor else {}),
                headers=headers,
            )
            assert page.status_code == 200, page.text
            body = page.json()
            assert body["counts"] == {
                "safety": 1,
                "flag": 2,
                "kb_review": 1,
                "release_ready": 1,
                "total": 5,
            }
            items.extend(body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert len({(i["kind"], i["item_id"]) for i in items}) == 5
        assert items == sorted(
            items,
            key=lambda i: (
                -datetime.fromisoformat(i["created_at"]).timestamp(),
                i["kind"],
                i["item_id"],
            ),
        )
        review = next(i for i in items if i["kind"] == "kb_review")
        assert (review["pending_concepts"], review["pending_misconceptions"]) == (2, 1)
        safety = next(i for i in items if i["kind"] == "safety")
        assert datetime.fromisoformat(safety["paused_at"]) == datetime(2026, 1, 1, tzinfo=UTC)
        assert safety["student_name"] == "Siswa Uji"
        assert (
            await api.post(
                f"/sessions/{world.session_id}/safety-actions",
                json={"action": "resume"},
                headers=headers,
            )
        ).status_code == 200
        assert (
            await conn.fetchval(
                "select safety_paused_at from sessions where id = $1", world.session_id
            )
            is None
        )
        for flag in flags:
            assert (
                await api.post(
                    f"/flags/{flag}/review",
                    json={"decision": "cleared"},
                    headers=headers,
                )
            ).status_code == 200
        for concept in concepts:
            assert (
                await api.post(
                    f"/concepts/{concept}/review",
                    json={"review_status": "approved"},
                    headers=headers,
                )
            ).status_code == 200
        assert (
            await api.post(
                f"/misconceptions/{misconception}/review",
                json={"review_status": "approved"},
                headers=headers,
            )
        ).status_code == 200
        assert (
            await api.post(
                f"/publications/{publication}/release",
                json={"expected_eligible_count": 1},
                headers=headers,
            )
        ).status_code == 200
        empty = await api.get("/teacher/attention", params=params, headers=headers)
        assert empty.json() == {
            "items": [],
            "counts": {"safety": 0, "flag": 0, "kb_review": 0, "release_ready": 0, "total": 0},
            "next_cursor": None,
        }


async def test_attention_excludes_other_teacher_and_school_data(
    conn: asyncpg.Connection, world: World
) -> None:
    await PgSessionsRepo(conn).pause_for_safety(world.session_id)
    await add_flag(conn, world, world.session_id)
    await create_concept(conn, world.school_id, world.kb_id, "pending")
    colleague = await create_teacher(conn, world.school_id)
    other = await build_world(conn, "Other school")
    await PgSessionsRepo(conn).pause_for_safety(other.session_id)
    await add_flag(conn, other, other.session_id)
    await create_concept(conn, other.school_id, other.kb_id, "pending")
    async with api_client(conn) as api:
        for actor in (world.student_id, world.admin_id, other.teacher_id):
            response = await api.get(
                "/teacher/attention",
                params={"school_id": str(world.school_id)},
                headers=as_user(actor),
            )
            assert response.status_code == 404
        empty = await api.get(
            "/teacher/attention",
            params={"school_id": str(world.school_id)},
            headers=as_user(colleague),
        )
        assert empty.json()["counts"]["total"] == 0
        assert empty.json()["items"] == []
        mine = await api.get(
            "/teacher/attention",
            params={"school_id": str(world.school_id)},
            headers=as_user(world.teacher_id),
        )
        assert mine.json()["counts"]["total"] == 3
        await conn.execute(
            "update school_memberships set status = 'inactive' where user_id = $1", world.teacher_id
        )
        inactive = await api.get(
            "/teacher/attention",
            params={"school_id": str(world.school_id)},
            headers=as_user(world.teacher_id),
        )
        assert inactive.status_code == 404


async def test_attention_rejects_wrong_school_cursor_and_preserves_unknown_legacy_pause(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "update sessions set status = 'paused_safety' where id = $1", world.session_id
    )
    await add_flag(conn, world, world.session_id)
    other = await build_world(conn)
    await add_membership(conn, other.school_id, world.teacher_id, "teacher")
    async with api_client(conn) as api:
        first = await api.get(
            "/teacher/attention",
            params={"school_id": str(world.school_id), "limit": 1},
            headers=as_user(world.teacher_id),
        )
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        wrong = await api.get(
            "/teacher/attention",
            params={"school_id": str(other.school_id), "cursor": cursor},
            headers=as_user(world.teacher_id),
        )
        assert wrong.status_code == 400
        invalid = await api.get(
            "/teacher/attention",
            params={"school_id": str(world.school_id), "cursor": "bad"},
            headers=as_user(world.teacher_id),
        )
        assert invalid.status_code == 400
        all_items = await api.get(
            "/teacher/attention",
            params={"school_id": str(world.school_id)},
            headers=as_user(world.teacher_id),
        )
        safety = next(i for i in all_items.json()["items"] if i["kind"] == "safety")
        assert safety["paused_at"] is None
        await conn.execute("update classes set archived_at = now() where id = $1", world.class_id)
        archived = await api.get(
            "/teacher/attention",
            params={"school_id": str(world.school_id)},
            headers=as_user(world.teacher_id),
        )
        assert archived.json()["counts"]["total"] == 0


@pytest.mark.parametrize(
    "blocker", ["open_run", "active", "waiting", "unevaluated", "no_eligible", "cancelled"]
)
async def test_attention_never_marks_blocked_publication_release_ready(
    conn: asyncpg.Connection, world: World, blocker: str
) -> None:
    await settled(conn, world)
    if blocker == "open_run":
        await conn.execute(
            "update publication_runs set status = 'open' where id = $1", world.run_id
        )
    elif blocker == "active":
        await student_session(conn, world)
    elif blocker == "waiting":
        await conn.execute(
            "insert into run_participants (school_id, run_id, student_id) values ($1, $2, $3)",
            world.school_id,
            world.run_id,
            world.student_id,
        )
    elif blocker == "unevaluated":
        await conn.execute(
            "delete from session_evaluations where session_id = $1", world.session_id
        )
    elif blocker == "no_eligible":
        await conn.execute(
            "update session_evaluations set status = 'failed' where session_id = $1",
            world.session_id,
        )
    else:
        await conn.execute(
            "update publications set cancelled_at = now() where id = $1", world.publication_id
        )
    async with api_client(conn) as api:
        response = await api.get(
            "/teacher/attention",
            params={"school_id": str(world.school_id)},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["release_ready"] == 0
