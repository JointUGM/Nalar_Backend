from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from nalar.application.features.administration.commands.process_reference import (
    ProcessReferenceHandler,
)
from nalar.application.ports.ai import AiServiceError
from nalar.application.ports.national_references import ReferencePolicy
from nalar.infrastructure.db.repositories.national_references import PgNationalReferencesRepo
from nalar.infrastructure.documents.reference_pdf import NationalReferencePdf
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, create_user
from tests.integration.support.uow import uow_on
from tests.integration.test_curriculum_seed import embedded
from tests.unit.application.fakes import FakeStorage, ScriptedAiGateway, invocation

TEXT = "Peserta didik menjelaskan gaya. Peserta didik menjelaskan gerak."
POLICY = ReferencePolicy(50 * 1024 * 1024, 500, 2_000_000, 2048, 900, 3, "text-embedding-3-small")


def source_pdf(text: str = TEXT) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 30 750 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def curriculum_review(revision: int = 1) -> dict[str, Any]:
    return {
        "base_revision": revision,
        "curriculum": {
            "name": "CP IPA",
            "decree_code": str(uuid4()),
            "effective_on": "2025-07-16",
            "subjects": [
                {
                    "name": "IPA",
                    "phase": "D",
                    "elements": [
                        {
                            "element": "Pemahaman IPA",
                            "description": TEXT,
                            "page_start": 1,
                            "page_end": 1,
                            "statements": [
                                {
                                    "description": "Peserta didik menjelaskan gaya.",
                                    "page_start": 1,
                                    "page_end": 1,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }


async def prepare(
    conn: asyncpg.Connection,
    kind: str = "curriculum",
) -> tuple[UUID, UUID, FakeStorage, ScriptedAiGateway, ProcessReferenceHandler]:
    admin = await create_user(conn)
    await conn.execute("update profiles set is_platform_admin=true where id=$1", admin)
    storage, ai = FakeStorage(), ScriptedAiGateway()
    worker = ProcessReferenceHandler(
        uow_on(conn), storage, NationalReferencePdf(POLICY), ai, POLICY
    )
    async with api_client(conn, storage=storage, ai=ai) as api:
        response = await api.post(
            "/platform/references",
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
            data={
                "kind": kind,
                "title": "CP IPA",
                "issuer": "BSKAP",
                "source_url": "https://example.go.id/cp.pdf",
            },
            files={"file": ("cp.pdf", source_pdf(), "application/pdf")},
        )
        assert response.status_code == 202, response.text
        queued = response.json()
    document_id, job_id = UUID(queued["document_id"]), UUID(queued["job_id"])
    await worker.execute(document_id, job_id)
    return admin, document_id, storage, ai, worker


async def test_platform_only_upload_and_review_scope(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    async with api_client(conn, storage=storage) as api:
        for actor in (world.teacher_id, world.admin_id, world.student_id, world.parent_id):
            response = await api.post(
                "/platform/references",
                headers=as_user(actor) | {"Idempotency-Key": str(uuid4())},
                data={
                    "kind": "curriculum",
                    "title": "CP",
                    "issuer": "BSKAP",
                    "source_url": "https://example.go.id/cp.pdf",
                },
                files={"file": ("cp.pdf", source_pdf(), "application/pdf")},
            )
            assert response.status_code == 404
            assert (
                await api.get("/platform/references", headers=as_user(actor))
            ).status_code == 404
    assert storage.objects == {}
    assert await conn.fetchval("select count(*) from national_reference_documents") == 0


async def test_upload_replay_has_one_document_and_one_job(conn: asyncpg.Connection) -> None:
    admin = await create_user(conn)
    await conn.execute("update profiles set is_platform_admin=true where id=$1", admin)
    storage = FakeStorage()
    headers = as_user(admin) | {"Idempotency-Key": str(uuid4())}
    async with api_client(conn, storage=storage) as api:
        receipts = []
        for _ in range(2):
            response = await api.post(
                "/platform/references",
                headers=headers,
                data={
                    "kind": "curriculum",
                    "title": "CP",
                    "issuer": "BSKAP",
                    "source_url": "https://example.go.id/cp.pdf",
                },
                files={"file": ("cp.pdf", source_pdf(), "application/pdf")},
            )
            assert response.status_code == 202, response.text
            receipts.append(response.json())
        changed = await api.post(
            "/platform/references",
            headers=headers,
            data={
                "kind": "curriculum",
                "title": "Different",
                "issuer": "BSKAP",
                "source_url": "https://example.go.id/cp.pdf",
            },
            files={"file": ("cp.pdf", source_pdf(), "application/pdf")},
        )
    assert receipts[0] == receipts[1]
    assert changed.status_code == 409
    assert len(storage.objects) == 1
    assert (
        await conn.fetchval("select count(*) from jobs where kind='national_reference_extract'")
        == 1
    )


async def test_curriculum_publish_is_indexed_once_and_preserves_missions(
    conn: asyncpg.Connection, world: World
) -> None:
    admin, document_id, storage, ai, worker = await prepare(conn)
    frozen = await conn.fetchval(
        "select context_pack from mission_versions where id=$1", world.version_id
    )
    async with api_client(conn, storage=storage, ai=ai) as api:
        detail = await api.get(f"/platform/references/{document_id}", headers=as_user(admin))
        assert detail.status_code == 200, detail.text
        assert TEXT in detail.json()["pages"][0]["text"]
        review = await api.put(
            f"/platform/references/{document_id}/review",
            json=curriculum_review(),
            headers=as_user(admin),
        )
        assert review.status_code == 200, review.text
        headers = as_user(admin) | {"Idempotency-Key": str(uuid4())}
        url = f"/platform/references/{document_id}/publish"
        queued = await api.post(url, json={"base_revision": 2}, headers=headers)
        assert queued.status_code == 202, queued.text
        assert (
            await api.post(url, json={"base_revision": 2}, headers=headers)
        ).json() == queued.json()
        job_id = UUID(queued.json()["job_id"])
        ai.script("embed", embedded(1))
        await worker.execute(document_id, job_id)
        await worker.execute(document_id, job_id)
        done = (await api.get(f"/platform/references/{document_id}", headers=as_user(admin))).json()
        assert done["status"] == "published"
        assert (await api.get(f"/jobs/{job_id}", headers=as_user(admin))).status_code == 200
        assert (
            await api.get(f"/jobs/{job_id}", headers=as_user(world.teacher_id))
        ).status_code == 404
        assert (
            await api.put(
                f"/platform/references/{document_id}/review",
                json=curriculum_review(2),
                headers=as_user(admin),
            )
        ).status_code == 409
    assert len(ai.calls) == 1
    outcomes = await conn.fetch(
        "select grain,embedding_model,source_page_start,parent_id from cp_learning_outcomes"
        " where source_document_id=$1 order by grain",
        document_id,
    )
    assert len(outcomes) == 2
    assert outcomes[1]["grain"] == "statement" and outcomes[1]["parent_id"] is not None
    assert outcomes[1]["embedding_model"] == POLICY.embedding_model
    assert await conn.fetchval("select count(*) from ai_invocations where purpose='embedding'") == 1
    assert (
        await conn.fetchval(
            "select context_pack from mission_versions where id=$1", world.version_id
        )
        == frozen
    )
    assert (
        await conn.fetchval(
            "select cp_subject_id from school_subjects where id=$1", world.subject_id
        )
        is None
    )


async def test_review_rejects_invented_outcomes_and_stale_revision(
    conn: asyncpg.Connection,
) -> None:
    admin, document_id, storage, _, _ = await prepare(conn)
    draft = curriculum_review()
    draft["curriculum"]["subjects"][0]["elements"][0]["statements"][0]["description"] = (
        "Invented outcome."
    )
    async with api_client(conn, storage=storage) as api:
        invalid = await api.put(
            f"/platform/references/{document_id}/review", json=draft, headers=as_user(admin)
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "SOURCE_TEXT_MISMATCH"
        stale = await api.put(
            f"/platform/references/{document_id}/review",
            json=curriculum_review(0),
            headers=as_user(admin),
        )
        assert stale.status_code == 409


async def test_embedding_failure_records_provenance_without_publishing(
    conn: asyncpg.Connection,
) -> None:
    admin, document_id, storage, ai, worker = await prepare(conn)
    async with api_client(conn, storage=storage, ai=ai) as api:
        await api.put(
            f"/platform/references/{document_id}/review",
            json=curriculum_review(),
            headers=as_user(admin),
        )
        response = await api.post(
            f"/platform/references/{document_id}/publish",
            json={"base_revision": 2},
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
        job_id = UUID(response.json()["job_id"])
    ai.script("embed", AiServiceError("invalid_input", 422, [invocation("embedding", "error")]))
    await worker.execute(document_id, job_id)
    assert (
        await conn.fetchval(
            "select status from national_reference_documents where id=$1", document_id
        )
        == "failed"
    )
    assert (
        await conn.fetchval(
            "select count(*) from cp_learning_outcomes where source_document_id=$1", document_id
        )
        == 0
    )
    assert await conn.fetchval("select count(*) from ai_invocations where status='failed'") == 1


async def test_expired_worker_cannot_write_over_new_claim(conn: asyncpg.Connection) -> None:
    admin, document_id, storage, _, _ = await prepare(conn)
    await conn.execute(
        "update national_reference_documents set status='extracting' where id=$1", document_id
    )
    repo = PgNationalReferencesRepo(conn)
    row = await repo.get(document_id)
    assert row is not None
    token1, token2 = uuid4(), uuid4()
    assert await repo.claim(document_id, row["job_id"], token1, 900)
    assert not await repo.claim(document_id, row["job_id"], token2, 900)
    await conn.execute(
        "update national_reference_documents set lease_until=now()-interval '1 second' where id=$1",
        document_id,
    )
    assert await repo.claim(document_id, row["job_id"], token2, 900)
    assert not await repo.extracted(document_id, token1, {1: "Wrong"})
    assert await repo.extracted(document_id, token2, {1: TEXT})


async def test_guidance_adoption_is_school_scoped_and_duplicate_safe(
    conn: asyncpg.Connection, world: World
) -> None:
    admin, document_id, storage, ai, worker = await prepare(conn, "guidance")
    async with api_client(conn, storage=storage, ai=ai) as api:
        hidden = await api.get(
            f"/schools/{world.school_id}/national-references", headers=as_user(world.teacher_id)
        )
        assert hidden.json() == []
        assert (
            await api.get(
                f"/schools/{world.school_id}/national-references", headers=as_user(world.student_id)
            )
        ).status_code == 404
        review = await api.put(
            f"/platform/references/{document_id}/review",
            json={"base_revision": 1, "selected_pages": [1]},
            headers=as_user(admin),
        )
        assert review.status_code == 200, review.text
        queued = await api.post(
            f"/platform/references/{document_id}/publish",
            json={"base_revision": 2},
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
        await worker.execute(document_id, UUID(queued.json()["job_id"]))
        visible = await api.get(
            f"/schools/{world.school_id}/national-references", headers=as_user(world.teacher_id)
        )
        assert len(visible.json()) == 1 and "pages" not in visible.json()[0]
        for actor in (world.admin_id, world.student_id, world.parent_id):
            denied = await api.post(
                f"/knowledge-bases/{world.kb_id}/national-references/{document_id}",
                headers=as_user(actor) | {"Idempotency-Key": str(uuid4())},
            )
            assert denied.status_code == 404
        assert (
            await conn.fetchval(
                "select count(*) from teaching_materials where national_reference_id=$1",
                document_id,
            )
            == 0
        )
        receipts = []
        for _ in range(2):
            adopted = await api.post(
                f"/knowledge-bases/{world.kb_id}/national-references/{document_id}",
                headers=as_user(world.teacher_id) | {"Idempotency-Key": str(uuid4())},
            )
            assert adopted.status_code == 202, adopted.text
            receipts.append(adopted.json())
    assert receipts[0] == receipts[1]
    material = await conn.fetchrow(
        "select school_id,national_source_pages from teaching_materials"
        " where national_reference_id=$1",
        document_id,
    )
    assert (
        material is not None
        and material["school_id"] == world.school_id
        and material["national_source_pages"] == [1]
    )
    assert (
        await conn.fetchval(
            "select count(*) from jobs where kind='kb_detect_sections' and entity_id=$1",
            UUID(receipts[0]["material_id"]),
        )
        == 1
    )
    assert ai.calls == []


async def test_reference_tables_are_not_exposed_to_browser_roles(conn: asyncpg.Connection) -> None:
    for role in ("anon", "authenticated"):
        for table in ("national_reference_documents", "national_reference_pages"):
            assert not await conn.fetchval(
                "select has_table_privilege($1,$2,'SELECT')", role, table
            )


async def test_transient_embedding_failure_requeues_and_manual_retry_recovers(
    conn: asyncpg.Connection,
) -> None:
    admin, document_id, storage, ai, worker = await prepare(conn)
    async with api_client(conn, storage=storage, ai=ai) as api:
        await api.put(
            f"/platform/references/{document_id}/review",
            json=curriculum_review(),
            headers=as_user(admin),
        )
        queued = await api.post(
            f"/platform/references/{document_id}/publish",
            json={"base_revision": 2},
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
        job_id = UUID(queued.json()["job_id"])
        ai.script(
            "embed",
            *[
                AiServiceError("unavailable", 503, [invocation("embedding", "error")])
                for _ in range(3)
            ],
        )
        for attempt in range(3):
            if attempt < 2:
                with pytest.raises(AiServiceError):
                    await worker.execute(document_id, job_id)
            else:
                await worker.execute(document_id, job_id)
            assert (
                await conn.fetchval(
                    "select lease_token from national_reference_documents where id=$1", document_id
                )
                is None
            )
        assert await conn.fetchval("select status from jobs where id=$1", job_id) == "failed"
        assert await conn.fetchval("select count(*) from ai_invocations where status='failed'") == 3
        retry = await api.post(
            f"/platform/references/{document_id}/retry",
            json={"base_revision": 2},
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
        assert retry.status_code == 202, retry.text
        ai.script("embed", embedded(1))
        await worker.execute(document_id, UUID(retry.json()["job_id"]))
        assert (
            await conn.fetchval(
                "select status from national_reference_documents where id=$1", document_id
            )
            == "published"
        )


@pytest.mark.parametrize("invalid", ["model", "nonfinite"])
async def test_invalid_embeddings_never_publish(conn: asyncpg.Connection, invalid: str) -> None:
    admin, document_id, storage, ai, worker = await prepare(conn)
    async with api_client(conn, storage=storage, ai=ai) as api:
        await api.put(
            f"/platform/references/{document_id}/review",
            json=curriculum_review(),
            headers=as_user(admin),
        )
        queued = await api.post(
            f"/platform/references/{document_id}/publish",
            json={"base_revision": 2},
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
    reply = embedded(1)
    reply.invocations[0].request_id = str(document_id)
    if invalid == "model":
        reply.result.embedding_model = "different-model"
    else:
        reply.result.vectors[0][0] = float("nan")
    ai.script("embed", reply)
    await worker.execute(document_id, UUID(queued.json()["job_id"]))
    assert (
        await conn.fetchval(
            "select status from national_reference_documents where id=$1", document_id
        )
        == "failed"
    )
    assert (
        await conn.fetchval(
            "select count(*) from cp_learning_outcomes where source_document_id=$1", document_id
        )
        == 0
    )
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where status='succeeded' and request_id=$1",
            str(document_id),
        )
        == 1
    )


async def test_published_pages_cannot_be_mutated_or_moved_in(conn: asyncpg.Connection) -> None:
    admin, document_id, storage, _, worker = await prepare(conn, "guidance")
    async with api_client(conn, storage=storage) as api:
        await api.put(
            f"/platform/references/{document_id}/review",
            json={"base_revision": 1, "selected_pages": [1]},
            headers=as_user(admin),
        )
        queued = await api.post(
            f"/platform/references/{document_id}/publish",
            json={"base_revision": 2},
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
    await worker.execute(document_id, UUID(queued.json()["job_id"]))
    _, other_id, _, _, _ = await prepare(conn)
    for query, arguments in [
        ("update national_reference_pages set text='Changed' where document_id=$1", (document_id,)),
        ("delete from national_reference_pages where document_id=$1", (document_id,)),
        ("insert into national_reference_pages values($1,2,'Changed')", (document_id,)),
        (
            "update national_reference_pages set document_id=$1,page_number=2 where document_id=$2",
            (document_id, other_id),
        ),
    ]:
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            async with conn.transaction():
                await conn.execute(query, *arguments)


async def test_source_checksum_change_is_not_processed(conn: asyncpg.Connection) -> None:
    admin = await create_user(conn)
    await conn.execute("update profiles set is_platform_admin=true where id=$1", admin)
    storage, ai = FakeStorage(), ScriptedAiGateway()
    worker = ProcessReferenceHandler(
        uow_on(conn), storage, NationalReferencePdf(POLICY), ai, POLICY
    )
    async with api_client(conn, storage=storage, ai=ai) as api:
        queued = await api.post(
            "/platform/references",
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
            data={
                "kind": "curriculum",
                "title": "CP",
                "issuer": "BSKAP",
                "source_url": "https://example.go.id/cp.pdf",
            },
            files={"file": ("cp.pdf", source_pdf(), "application/pdf")},
        )
    document_id = UUID(queued.json()["document_id"])
    object_key = next(iter(storage.objects))
    storage.objects[object_key] = source_pdf("Different source")
    await worker.execute(document_id, UUID(queued.json()["job_id"]))
    assert (
        await conn.fetchval(
            "select error_code from national_reference_documents where id=$1", document_id
        )
        == "REFERENCE_SOURCE_CHANGED"
    )
    assert (
        await conn.fetchval(
            "select count(*) from national_reference_pages where document_id=$1", document_id
        )
        == 0
    )


async def test_textless_pdf_requires_ocr_instead_of_empty_grounding() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="REFERENCE_OCR_REQUIRED"):
        await NationalReferencePdf(POLICY).extract(output.getvalue())
