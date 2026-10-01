import json
from typing import Any
from uuid import UUID

import asyncpg

from nalar.application.features.knowledge_base.commands.detect_sections import (
    DetectSectionsHandler,
)
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import DetectSectionsOut
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world, create_teacher
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import FakeStorage, ScriptedAiGateway, invocation

PDF = b"%PDF-1.7\n" + b"x" * 64


def upload(
    world: World,
    title: str = "Zat dan Perubahannya",
    data: bytes = PDF,
) -> dict[str, Any]:
    return {
        "data": {"school_subject_id": str(world.subject_id), "topic_title": title},
        "files": {"file": ("bab1.pdf", data, "application/pdf")},
    }


def detection() -> AiResult[DetectSectionsOut]:
    return AiResult(
        DetectSectionsOut.model_validate(
            {
                "has_toc": True,
                "page_count": 40,
                "pages_without_text": [3, 17],
                "sections": [
                    {
                        "ordinal": 1,
                        "level": 1,
                        "title": "Bab 1 Zat",
                        "page_start": 1,
                        "page_end": 20,
                        "parent_ordinal": None,
                        "suggested": True,
                    },
                    {
                        "ordinal": 2,
                        "level": 2,
                        "title": "1.1 Wujud Zat",
                        "page_start": 2,
                        "page_end": 9,
                        "parent_ordinal": 1,
                        "suggested": False,
                    },
                ],
            }
        ),
        [invocation("kb_extract")],
    )


async def uploaded(conn: asyncpg.Connection, world: World, storage: FakeStorage) -> dict[str, str]:
    async with api_client(conn, storage=storage) as api:
        response = await api.post(
            f"/schools/{world.school_id}/knowledge-bases",
            headers=as_user(world.teacher_id),
            **upload(world),
        )
    assert response.status_code == 202
    body: dict[str, str] = response.json()
    return body


async def test_upload_creates_kb_material_job_and_detect_message(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    before = await conn.fetchval("select coalesce(max(msg_id), 0) from pgmq.q_nalar_kb")
    body = await uploaded(conn, world, storage)
    kb = await conn.fetchrow(
        "select topic_key, owner_teacher_id from knowledge_bases where id = $1",
        UUID(body["knowledge_base_id"]),
    )
    assert kb is not None
    assert (kb["topic_key"], kb["owner_teacher_id"]) == ("zat-dan-perubahannya", world.teacher_id)
    assert list(storage.objects.values()) == [PDF]
    messages = await conn.fetch("select message from pgmq.q_nalar_kb where msg_id > $1", before)
    assert [json.loads(m["message"]) for m in messages] == [
        {"kind": "kb_detect_sections", "material_id": body["material_id"], "job_id": body["job_id"]}
    ]


async def test_existing_topic_returns_409(conn: asyncpg.Connection, world: World) -> None:
    async with api_client(conn) as api:
        response = await api.post(
            f"/schools/{world.school_id}/knowledge-bases",
            headers=as_user(world.teacher_id),
            **upload(world, title="Gaya dan Gerak"),
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TOPIC_ALREADY_EXISTS"


async def test_non_pdf_and_oversized_files_are_rejected(
    conn: asyncpg.Connection, world: World
) -> None:
    async with api_client(conn) as api:
        not_pdf = await api.post(
            f"/schools/{world.school_id}/knowledge-bases",
            headers=as_user(world.teacher_id),
            **upload(world, data=b"PK\x03\x04 zip"),
        )
        too_big = await api.post(
            f"/schools/{world.school_id}/knowledge-bases",
            headers=as_user(world.teacher_id),
            **upload(world, data=PDF + b"x" * (50 * 1024 * 1024)),
        )
    assert (not_pdf.status_code, not_pdf.json()["error"]["code"]) == (400, "FILE_NOT_PDF")
    assert (too_big.status_code, too_big.json()["error"]["code"]) == (400, "FILE_TOO_LARGE")


async def test_teacher_without_the_subject_gets_403(conn: asyncpg.Connection, world: World) -> None:
    colleague = await create_teacher(conn, world.school_id)
    async with api_client(conn) as api:
        response = await api.post(
            f"/schools/{world.school_id}/knowledge-bases",
            headers=as_user(colleague),
            **upload(world),
        )
    assert (response.status_code, response.json()["error"]["code"]) == (
        403,
        "NOT_ASSIGNED_TO_SUBJECT",
    )


async def test_other_school_upload_returns_404(conn: asyncpg.Connection, world: World) -> None:
    intruder = await build_world(conn, "SMP Penyusup")
    async with api_client(conn) as api:
        new_kb = await api.post(
            f"/schools/{world.school_id}/knowledge-bases",
            headers=as_user(intruder.teacher_id),
            **upload(world),
        )
        more = await api.post(
            f"/knowledge-bases/{world.kb_id}/materials",
            headers=as_user(intruder.teacher_id),
            files={"file": ("bab2.pdf", PDF, "application/pdf")},
        )
    assert (new_kb.status_code, more.status_code) == (404, 404)


async def test_detection_stores_sections_and_reports_pages_without_text(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    body = await uploaded(conn, world, storage)
    ai = ScriptedAiGateway()
    ai.script("detect_sections", detection())
    handler = DetectSectionsHandler(uow_on(conn), ai, storage)
    await handler.execute(UUID(body["material_id"]), UUID(body["job_id"]))
    await handler.execute(UUID(body["material_id"]), UUID(body["job_id"]))
    async with api_client(conn, storage=storage) as api:
        kb = await api.get(
            f"/knowledge-bases/{body['knowledge_base_id']}", headers=as_user(world.teacher_id)
        )
        sections = await api.get(
            f"/knowledge-bases/{body['knowledge_base_id']}/sections",
            headers=as_user(world.teacher_id),
        )
        job = await api.get(f"/jobs/{body['job_id']}", headers=as_user(world.teacher_id))
    assert kb.json()["materials"][0]["pages_without_text"] == [3, 17]
    items = sections.json()["items"]
    assert [(s["ordinal"], s["build_status"], s["suggested"]) for s in items] == [
        (1, "not_selected", True),
        (2, "not_selected", False),
    ]
    assert items[1]["parent_section_id"] == items[0]["id"]
    assert job.json()["status"] == "succeeded"
    assert len(ai.calls) == 1
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1", world.school_id
        )
        == 1
    )


async def test_unreadable_pdf_fails_the_job_and_keeps_the_invocations(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    body = await uploaded(conn, world, storage)
    ai = ScriptedAiGateway()
    ai.script(
        "detect_sections",
        AiServiceError("document_unreadable", 422, [invocation("kb_extract", "error")]),
    )
    await DetectSectionsHandler(uow_on(conn), ai, storage).execute(
        UUID(body["material_id"]), UUID(body["job_id"])
    )
    job = await conn.fetchrow(
        "select status::text, error_code from jobs where id = $1", UUID(body["job_id"])
    )
    assert job is not None
    assert (job["status"], job["error_code"]) == ("failed", "document_unreadable")
    assert (
        await conn.fetchval(
            "select processing_status::text from teaching_materials where id = $1",
            UUID(body["material_id"]),
        )
        == "failed"
    )
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where school_id = $1 and status = 'failed'",
            world.school_id,
        )
        == 1
    )
