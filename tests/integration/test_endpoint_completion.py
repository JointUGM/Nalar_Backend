import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    add_score,
    build_world,
    create_mission_version,
    create_student,
    create_user,
)
from tests.integration.test_kb_upload import PDF, upload
from tests.integration.test_release import settled
from tests.unit.application.fakes import FakeClock, FakeStorage


@pytest.mark.parametrize("kind", ["kb", "material", "mission"])
async def test_creation_replays_and_rejects_changed_body(
    conn: asyncpg.Connection, world: World, kind: str
) -> None:
    headers = {**as_user(world.teacher_id), "Idempotency-Key": str(uuid4())}
    async with api_client(conn) as api:
        if kind == "kb":
            url = f"/schools/{world.school_id}/knowledge-bases"
            body = upload(world)
            changed = upload(world, "Different title")
        elif kind == "material":
            url = f"/knowledge-bases/{world.kb_id}/materials"
            body = {"files": {"file": ("same.pdf", PDF, "application/pdf")}}
            changed = {"files": {"file": ("other.pdf", PDF, "application/pdf")}}
        else:
            url = "/missions"
            body = {
                "json": {
                    "knowledge_base_id": str(world.kb_id),
                    "title": "Retry",
                    "learning_objective": "Reason about friction",
                }
            }
            changed = {"json": {**body["json"], "title": "Changed"}}
        first = await api.post(url, headers=headers, **body)
        assert first.status_code in (201, 202), first.text
        replay = await api.post(url, headers=headers, **body)
        assert replay.json() == first.json(), replay.text
        conflict = await api.post(url, headers=headers, **changed)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_publish_replay_does_not_replace_a_cancelled_publication(
    conn: asyncpg.Connection, world: World
) -> None:
    _, version_id = await create_mission_version(
        conn, world.school_id, world.kb_id, world.teacher_id
    )
    headers = {**as_user(world.teacher_id), "Idempotency-Key": str(uuid4())}
    body = {
        "mission_version_id": str(version_id),
        "class_id": str(world.class_id),
        "run": {"mode": "live"},
    }
    async with api_client(conn) as api:
        first = await api.post("/publications", json=body, headers=headers)
        assert first.status_code == 201, first.text
        assert (
            await api.post(
                f"/publications/{first.json()['publication_id']}/cancel", headers=headers
            )
        ).status_code == 204
        replay = await api.post("/publications", json=body, headers=headers)
        assert replay.json() == first.json()
        changed = await api.post(
            "/publications",
            json={**body, "run": {"mode": "live", "planner_mode": "hybrid"}},
            headers=headers,
        )
        assert changed.status_code == 409
        assert (
            await conn.fetchval(
                "select count(*) from publications where mission_version_id = $1", version_id
            )
            == 1
        )


async def test_filters_apply_before_page_limit_and_preserve_school_scope(
    conn: asyncpg.Connection, world: World
) -> None:
    foreign = await build_world(conn, "Foreign")
    async with api_client(conn) as api:
        headers = as_user(world.teacher_id)
        for path in (
            f"/schools/{world.school_id}/missions",
            f"/schools/{world.school_id}/knowledge-bases",
            "/teacher/publications",
        ):
            response = await api.get(
                path, params={"q": "unmatched title", "limit": 1}, headers=headers
            )
            assert response.status_code == 200, response.text
            assert response.json()["items"] == []
        denied = await api.get(f"/schools/{foreign.school_id}/missions", headers=headers)
        assert denied.status_code == 404
        pubs = await api.get(
            "/teacher/publications",
            params={"school_subject_id": str(world.subject_id)},
            headers=headers,
        )
        assert pubs.status_code == 200, pubs.text
        assert pubs.json()["items"][0]["mission_id"] == str(world.mission_id)
        assert pubs.json()["items"][0]["subject_name"] == "IPA"


async def test_archiving_retains_published_reports_and_blocks_new_authoring(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        headers = as_user(world.teacher_id)
        foreign = await build_world(conn, "Foreign")
        assert (
            await api.post(
                f"/missions/{world.mission_id}/archive", headers=as_user(foreign.teacher_id)
            )
        ).status_code == 404
        for _ in range(2):
            response = await api.post(f"/missions/{world.mission_id}/archive", headers=headers)
            assert response.status_code == 204, response.text
        assert (
            await api.get(f"/sessions/{world.session_id}/report", headers=headers)
        ).status_code == 200
        concept = await api.post(
            f"/knowledge-bases/{world.kb_id}/concepts/{world.concept_id}/archive", headers=headers
        )
        assert concept.status_code == 204, concept.text
        archived = await api.post(f"/knowledge-bases/{world.kb_id}/archive", headers=headers)
        assert archived.status_code == 204, archived.text
        blocked = await api.post(
            "/missions",
            headers=headers,
            json={
                "knowledge_base_id": str(world.kb_id),
                "title": "Blocked",
                "learning_objective": "Reason",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert (
            await api.get(f"/sessions/{world.session_id}/report", headers=headers)
        ).status_code == 200


async def test_teacher_end_is_scoped_and_queues_evaluation_once(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    before = await conn.fetchval("select count(*) from pgmq.q_nalar_eval")
    async with api_client(conn, clock=clock) as api:
        url = f"/sessions/{world.session_id}/end"
        assert (await api.post(url, headers=as_user(world.student_id))).status_code == 404
        for _ in range(2):
            response = await api.post(url, headers=as_user(world.teacher_id))
            assert response.status_code == 204, response.text
    assert await conn.fetchval("select count(*) from pgmq.q_nalar_eval") == before + 1
    row = await conn.fetchrow(
        "select status::text,end_reason::text from sessions where id = $1", world.session_id
    )
    assert row is not None
    assert dict(row) == {"status": "timed_out", "end_reason": "teacher_ended"}


async def test_inbox_does_not_expose_invitation_payload_or_revoked_scope(
    conn: asyncpg.Connection, world: World
) -> None:
    notification_id = await conn.fetchval(
        "insert into notifications(recipient_id,school_id,type,payload,dedupe_key)"
        " values($1,$2,'account_invitation',$3::jsonb,$4) returning id",
        world.parent_id,
        world.school_id,
        json.dumps({"proof": "SECRET", "url": "SECRET"}),
        str(uuid4()),
    )
    async with api_client(conn) as api:
        headers = as_user(world.parent_id)
        inbox = await api.get("/notifications", headers=headers)
        assert inbox.status_code == 200, inbox.text
        assert inbox.json()["unread_count"] == 1
        assert "SECRET" not in inbox.text and "payload" not in inbox.text
        for _ in range(2):
            assert (
                await api.post(f"/notifications/{notification_id}/read", headers=headers)
            ).status_code == 204
        assert (
            await api.post(
                f"/notifications/{notification_id}/read", headers=as_user(world.student_id)
            )
        ).status_code == 404
        await conn.execute(
            "update parent_student_links set deactivated_at = now() where parent_id = $1",
            world.parent_id,
        )
        assert (await api.get("/notifications", headers=headers)).status_code == 404


async def test_parent_seen_and_subject_projection_never_include_unreleased_results(
    conn: asyncpg.Connection, world: World
) -> None:
    await settled(conn, world)
    async with api_client(conn) as api:
        headers = as_user(world.parent_id)
        children = await api.get("/parent/children", headers=headers)
        assert children.status_code == 200, children.text
        child = children.json()["items"][0]
        assert child["class_name"] == "8A" and child["school_id"] == str(world.school_id)
        assert child["last_seen_at"] is None
        assert (
            await api.post(f"/parent/children/{world.student_id}/seen", headers=headers)
        ).status_code == 204
        reflections = await api.get(
            f"/parent/children/{world.student_id}/reflections", headers=headers
        )
        assert reflections.json()["items"] == []
        assert (await api.get("/parent/children", headers=headers)).json()["items"][0][
            "last_seen_at"
        ]


async def test_exports_escape_formulas_and_history_denies_other_teachers(
    conn: asyncpg.Connection, world: World
) -> None:
    await settled(conn, world)
    await add_score(conn, world, world.session_id)
    await conn.execute("update profiles set full_name = '=1+1' where id = $1", world.student_id)
    foreign = await build_world(conn, "Foreign")
    async with api_client(conn) as api:
        headers = as_user(world.teacher_id)
        exported = await api.get(f"/publications/{world.publication_id}/export", headers=headers)
        assert exported.status_code == 200, exported.text
        assert "'=1+1" in exported.text
        assert (
            await api.get(
                f"/sessions/{world.session_id}/report/export", headers=as_user(world.parent_id)
            )
        ).status_code == 404
        history = await api.get(f"/teacher/students/{world.student_id}/history", headers=headers)
        assert history.status_code == 200, history.text
        assert history.json()["items"][0]["scores"] == [{"dimension": "claim", "final_level": 2}]
        assert (
            await api.get(
                f"/teacher/students/{world.student_id}/history", headers=as_user(foreign.teacher_id)
            )
        ).status_code == 404


async def test_platform_reads_are_metadata_only_and_school_edits_are_scoped(
    conn: asyncpg.Connection, world: World
) -> None:
    platform = await create_user(conn)
    await conn.execute("update profiles set is_platform_admin = true where id = $1", platform)
    clock = FakeClock()
    async with api_client(conn, clock=clock) as api:
        headers = as_user(platform)
        url = f"/platform/schools/{world.school_id}"
        assert (
            await api.patch(url, headers=as_user(world.admin_id), json={"name": "Denied"})
        ).status_code == 404
        edited = await api.patch(url, headers=headers, json={"name": "New name"})
        assert edited.status_code == 200, edited.text
        assert edited.json()["name"] == "New name"
        audit = await api.get("/platform/audit-log", headers=headers)
        assert audit.status_code == 200, audit.text
        assert "changes" not in audit.json()["items"][0]
        usage = await api.get(
            "/platform/ai-usage",
            params={
                "from": (clock.now() - timedelta(days=1)).isoformat(),
                "to": clock.now().isoformat(),
            },
            headers=headers,
        )
        assert usage.status_code == 200, usage.text
        assert (
            await api.get(f"/schools/{world.school_id}/audit-log", headers=as_user(world.parent_id))
        ).status_code == 404


async def test_client_events_are_bounded_deduplicated_and_reject_raw_error_text(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    event = {
        "event_id": str(uuid4()),
        "kind": "error",
        "code": "route_render_failed",
        "route": "/teacher/missions",
        "occurred_at": clock.now().isoformat(),
    }
    async with api_client(conn, clock=clock) as api:
        headers = as_user(world.teacher_id)
        for _ in range(2):
            result = await api.post("/client-events", headers=headers, json={"events": [event]})
            assert result.status_code == 202, result.text
        assert (
            await conn.fetchval(
                "select count(*) from client_events where actor_id = $1", world.teacher_id
            )
            == 1
        )
        invalid = await api.post(
            "/client-events",
            headers=headers,
            json={"events": [{**event, "message": "private student answer"}]},
        )
        assert invalid.status_code == 400


async def test_signed_material_link_requires_scope_and_preserves_archived_citations(
    conn: asyncpg.Connection, world: World
) -> None:
    material_id = uuid4()
    await conn.execute(
        "insert into teaching_materials(id,school_id,knowledge_base_id,uploaded_by,title,"
        " storage_bucket,storage_path,size_bytes,mime_type)"
        " values($1,$2,$3,$4,'PDF','teaching-materials','test.pdf',9,'application/pdf')",
        material_id,
        world.school_id,
        world.kb_id,
        world.teacher_id,
    )
    async with api_client(conn, storage=FakeStorage()) as api:
        url = f"/knowledge-bases/{world.kb_id}/materials/{material_id}/file"
        assert (await api.get(url, headers=as_user(world.parent_id))).status_code == 404
        assert (
            await api.delete(url.removesuffix("/file"), headers=as_user(world.teacher_id))
        ).status_code == 204
        signed = await api.get(url, headers=as_user(world.teacher_id))
        assert signed.status_code == 200, signed.text
        assert signed.json()["expires_in"] == 300
        assert await conn.fetchval(
            "select archived_at is not null from teaching_materials where id = $1", material_id
        )


async def test_cancelled_lobby_participants_cannot_start_or_rejoin(
    conn: asyncpg.Connection, world: World
) -> None:
    student = await create_student(conn, world.school_id, world.class_id, world.year_id)
    await conn.execute(
        "update publication_runs set status = 'lobby', join_code = 'ABC234' where id = $1",
        world.run_id,
    )
    async with api_client(conn) as api:
        joined = await api.post(
            "/student/runs/join", headers=as_user(student), json={"join_code": "ABC234"}
        )
        assert joined.status_code == 200, joined.text
        participant_id = await conn.fetchval(
            "select id from run_participants where run_id = $1 and student_id = $2",
            world.run_id,
            student,
        )
        removed = await api.post(
            f"/runs/{world.run_id}/participants/{participant_id}/remove",
            headers=as_user(world.teacher_id),
        )
        assert removed.status_code == 204, removed.text
        assert (
            await api.post(
                "/student/runs/join", headers=as_user(student), json={"join_code": "ABC234"}
            )
        ).status_code == 409
        assert (
            await api.post(f"/runs/{world.run_id}/start", headers=as_user(world.teacher_id))
        ).status_code == 200
        assert not await conn.fetchval(
            "select exists(select 1 from sessions where run_id = $1 and student_id = $2)",
            world.run_id,
            student,
        )


async def test_upload_retry_reuses_object_after_storage_acknowledgement_is_lost(
    conn: asyncpg.Connection, world: World
) -> None:
    class InterruptedStorage(FakeStorage):
        fail = True

        async def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
            await super().upload(bucket, path, data, content_type)
            if self.fail:
                self.fail = False
                raise TimeoutError("upload acknowledgement lost")

    storage = InterruptedStorage()
    headers = {**as_user(world.teacher_id), "Idempotency-Key": str(uuid4())}
    async with api_client(conn, storage=storage) as api:
        url = f"/knowledge-bases/{world.kb_id}/materials"
        body: dict[str, Any] = {"files": {"file": ("retry.pdf", PDF, "application/pdf")}}
        assert (await api.post(url, headers=headers, **body)).status_code == 500
        recovered = await api.post(url, headers=headers, **body)
        assert recovered.status_code == 202, recovered.text
        replay = await api.post(url, headers=headers, **body)
        assert replay.json() == recovered.json()
    assert len(storage.objects) == 1
    assert (
        await conn.fetchval(
            "select count(*) from jobs where entity_id = $1", UUID(recovered.json()["material_id"])
        )
        == 1
    )


async def test_parent_digest_inbox_requires_currently_released_payload_items(
    conn: asyncpg.Connection, world: World
) -> None:
    await settled(conn, world)
    notification_id = await conn.fetchval(
        "insert into notifications(recipient_id,school_id,type,payload,dedupe_key)"
        " values($1,$2,'parent_periodic_summary',$3::jsonb,$4) returning id",
        world.parent_id,
        world.school_id,
        json.dumps(
            {
                "items": [
                    {
                        "student_id": str(world.student_id),
                        "publication_id": str(world.publication_id),
                    }
                ]
            }
        ),
        str(uuid4()),
    )
    async with api_client(conn) as api:
        headers = as_user(world.parent_id)
        hidden = await api.get("/notifications", headers=headers)
        assert hidden.status_code == 200, hidden.text
        assert hidden.json() == {"items": [], "unread_count": 0, "next_cursor": None}
        await conn.execute(
            "update publications set released_to_parents_at = now(), released_by = published_by"
            " where id = $1",
            world.publication_id,
        )
        visible = await api.get("/notifications", headers=headers)
        assert visible.json()["unread_count"] == 1
        assert visible.json()["items"][0]["id"] == str(notification_id)
        assert "publication_id" not in visible.text and "payload" not in visible.text


async def test_student_leave_only_cancels_own_waiting_participant(
    conn: asyncpg.Connection, world: World
) -> None:
    student = await create_student(conn, world.school_id, world.class_id, world.year_id)
    await conn.execute(
        "update publication_runs set status = 'lobby', join_code = 'ABC234' where id = $1",
        world.run_id,
    )
    async with api_client(conn) as api:
        headers = as_user(student)
        joined = await api.post("/student/runs/join", headers=headers, json={"join_code": "ABC234"})
        assert joined.status_code == 200, joined.text
        left = await api.post(f"/student/runs/{world.run_id}/leave", headers=headers)
        assert left.status_code == 204, left.text
        assert (
            await api.post(f"/student/runs/{world.run_id}/leave", headers=as_user(world.teacher_id))
        ).status_code == 404
        assert (
            await conn.fetchval(
                "select status::text from run_participants where run_id = $1 and student_id = $2",
                world.run_id,
                student,
            )
            == "cancelled"
        )


async def test_stored_suggestions_fill_sql_counts_without_generating_on_view(
    conn: asyncpg.Connection, world: World
) -> None:
    insight_id = await conn.fetchval(
        "insert into class_map_insights(school_id,publication_id,"
        " counts_snapshot,clusters,narrative)"
        " values($1,$2,$3::jsonb,'[]','Diskusikan alasan siswa.')"
        " returning id",
        world.school_id,
        world.publication_id,
        json.dumps({"denominator": 3, "incomplete_count": 0, "concepts": []}),
    )
    await conn.execute(
        "insert into follow_up_suggestions(school_id,insight_id,rank,content)"
        " values($1,$2,1,'Ajak {{total}} siswa membandingkan alasan.')",
        world.school_id,
        insight_id,
    )
    async with api_client(conn) as api:
        result = await api.get(
            f"/publications/{world.publication_id}/class-map", headers=as_user(world.teacher_id)
        )
        assert result.status_code == 200, result.text
        assert result.json()["insight"]["suggestions"] == ["Ajak 3 siswa membandingkan alasan."]
