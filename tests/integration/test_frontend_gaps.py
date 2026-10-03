from uuid import uuid4

import asyncpg

from tests.contract.forbidden import forbidden_keys
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    build_world,
    create_misconception,
    create_session,
    create_student,
    evaluate,
    finish_session,
)


async def test_monitor_session_is_latest_in_its_run(conn: asyncpg.Connection, world: World) -> None:
    absent = await create_student(conn, world.school_id, world.class_id, world.year_id)
    latest = await create_session(
        conn, world.school_id, world.publication_id, world.run_id, world.student_id, 2
    )
    other_run = await conn.fetchval(
        "insert into publication_runs(school_id,publication_id,kind,mode,status,opens_at,closes_at,"
        "grant_student_id,granted_by)"
        " values($1,$2,'grant','window','open',now(),now()+interval '1 hour',$3,$4) returning id",
        world.school_id,
        world.publication_id,
        world.student_id,
        world.teacher_id,
    )
    await create_session(
        conn, world.school_id, world.publication_id, other_run, world.student_id, 3
    )
    async with api_client(conn) as api:
        response = await api.get(
            f"/publications/{world.publication_id}/monitor", headers=as_user(world.teacher_id)
        )
    assert response.status_code == 200, response.text
    students = {item["student_id"]: item for item in response.json()["students"]}
    assert students[str(world.student_id)]["session_id"] == str(latest)
    assert students[str(absent)]["session_id"] is None


async def test_academic_years_require_school_admin_scope(
    conn: asyncpg.Connection, world: World
) -> None:
    other = await build_world(conn, "Other school")
    path = f"/schools/{world.school_id}/academic-years"
    async with api_client(conn) as api:
        response = await api.get(path, headers=as_user(world.admin_id))
        assert response.status_code == 200, response.text
        assert response.json() == [
            {
                "id": str(world.year_id),
                "name": "2026/2027",
                "starts_on": "2026-07-13",
                "ends_on": "2027-06-30",
                "is_current": True,
            }
        ]
        for actor in (world.teacher_id, world.student_id, other.admin_id):
            assert (await api.get(path, headers=as_user(actor))).status_code == 404
        await conn.execute(
            "update school_memberships set status='inactive' where user_id=$1", world.admin_id
        )
        assert (await api.get(path, headers=as_user(world.admin_id))).status_code == 404


async def test_student_reflections_are_persisted_owned_and_paginated(
    conn: asyncpg.Connection, world: World
) -> None:
    await finish_session(conn, world, world.session_id)
    await evaluate(conn, world, world.session_id)
    second = await create_session(
        conn, world.school_id, world.publication_id, world.run_id, world.student_id, 2
    )
    await finish_session(conn, world, second)
    await evaluate(conn, world, second)
    other = await build_world(conn, "Other school")
    await finish_session(conn, other, other.session_id)
    await evaluate(conn, other, other.session_id)
    async with api_client(conn) as api:
        first = await api.get("/student/reflections?limit=1", headers=as_user(world.student_id))
        assert first.status_code == 200, first.text
        assert first.json()["next_cursor"]
        next_page = await api.get(
            "/student/reflections",
            params={
                "limit": 1,
                "cursor": first.json()["next_cursor"],
            },
            headers=as_user(world.student_id),
        )
        pages = first.json()["items"] + next_page.json()["items"]
        assert {item["session_id"] for item in pages} == {str(world.session_id), str(second)}
        assert next_page.json()["next_cursor"] is None
        assert all(item["content"] == "Kamu sudah memikirkan gaya gesek." for item in pages)
        assert not forbidden_keys(set().union(*(item.keys() for item in pages)))
        await conn.execute(
            "update school_memberships set status='inactive' where user_id=$1", world.student_id
        )
        hidden = await api.get("/student/reflections", headers=as_user(world.student_id))
        assert hidden.json() == {"items": [], "next_cursor": None}


async def test_invitation_state_filter_keeps_counts_and_identity_scoped(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "update profiles set has_real_email=false where id=$1",
        world.student_id,
    )
    async with api_client(conn) as api:
        path = f"/schools/{world.school_id}/account-invitations"
        response = await api.get(
            path,
            params={"state": "requires_assistance", "limit": 1},
            headers=as_user(world.admin_id),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"][0]["state"] == "requires_assistance"
        assert body["items"][0]["role"] == "student"
        name = await conn.fetchval("select full_name from profiles where id=$1", world.student_id)
        assert body["items"][0]["full_name"] == name
        assert body["total"] == body["counts"]["requires_assistance"] == 1
        assert body["counts"]["not_requested"] >= 1
        assert body["next_cursor"] is None
        rejected = await api.get(path, params={"state": "invalid"}, headers=as_user(world.admin_id))
        assert rejected.status_code == 400
        hidden = await api.get(
            f"/schools/{uuid4()}/account-invitations?state=requires_assistance",
            headers=as_user(world.admin_id),
        )
        assert hidden.status_code == 404


async def test_kb_page_sources_exclude_other_kbs_and_archived_materials(
    conn: asyncpg.Connection, world: World
) -> None:
    other = await build_world(conn, "Other school")
    chunk_ids = []
    for index, owner in enumerate((world, world, other)):
        material = await conn.fetchval(
            "insert into teaching_materials"
            " (school_id,knowledge_base_id,uploaded_by,title,storage_path,mime_type)"
            " values($1,$2,$3,'source.pdf',$4,'application/pdf') returning id",
            owner.school_id,
            owner.kb_id,
            owner.teacher_id,
            str(uuid4()),
        )
        chunk = await conn.fetchval(
            "insert into material_chunks(school_id,knowledge_base_id,material_id,"
            "chunk_index,content,"
            "page_start,page_end) values($1,$2,$3,0,'source',$4,$5) returning id",
            owner.school_id,
            owner.kb_id,
            material,
            3 + index * 10,
            5 + index * 10,
        )
        chunk_ids.append(chunk)
        if index == 1:
            await conn.execute(
                "update teaching_materials set archived_at=now() where id=$1", material
            )
    await conn.execute(
        "update concepts set source_chunk_ids=$2 where id=$1", world.concept_ids[0], chunk_ids[:2]
    )
    misconception = await create_misconception(
        conn, world.school_id, world.kb_id, world.concept_ids[0]
    )
    await conn.execute(
        "update misconceptions set source_chunk_ids=$2 where id=$1", misconception, chunk_ids[:2]
    )
    async with api_client(conn) as api:
        response = await api.get(
            f"/knowledge-bases/{world.kb_id}", headers=as_user(world.teacher_id)
        )
    assert response.status_code == 200, response.text
    concept = next(c for c in response.json()["concepts"] if c["id"] == str(world.concept_ids[0]))
    item = next(m for m in response.json()["misconceptions"] if m["id"] == str(misconception))
    assert concept["sources"] == item["sources"] == [{"page_start": 3, "page_end": 5}]
