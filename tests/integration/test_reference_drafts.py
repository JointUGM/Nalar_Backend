import json
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from nalar.application.features.administration.commands.draft_reference import (
    DraftReferenceHandler,
)
from nalar.application.features.administration.commands.process_reference import (
    ProcessReferenceHandler,
)
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import CpExcerptDraft
from nalar.application.ports.national_references import ReferencePdf, ReferencePolicy
from nalar.infrastructure.documents.reference_pdf import NationalReferencePdf
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, create_user
from tests.integration.support.uow import uow_on
from tests.integration.test_national_references import POLICY, source_pdf
from tests.unit.application.fakes import FakeStorage, ScriptedAiGateway, invocation

CP_TEXT = "Pada akhir fase D, peserta didik menjelaskan gaya. Peserta didik menjelaskan gerak."
DRAFT_POLICY = replace(POLICY, ai_draft=True)
DRAFT_ENABLED = {"reference_ai_draft_enabled": True}


def excerpt(
    statements: list[str], decree: dict[str, Any] | None = None, element: str = "Pemahaman IPA"
) -> AiResult[CpExcerptDraft]:
    return AiResult(
        CpExcerptDraft.model_validate(
            {
                "decree_code": decree,
                "effective_on": None,
                "subjects": [
                    {
                        "name": "IPA",
                        "phase": "D",
                        "elements": [
                            {
                                "element": element,
                                "text": CP_TEXT,
                                "page_start": 1,
                                "page_end": 1,
                                "statements": [
                                    {"text": s, "page_start": 1, "page_end": 1} for s in statements
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        [invocation("cp_extract")],
    )


async def upload(
    conn: asyncpg.Connection,
    text: str = CP_TEXT,
    kind: str = "curriculum",
    policy: ReferencePolicy = DRAFT_POLICY,
    pdf: ReferencePdf | None = None,
) -> tuple[UUID, UUID, FakeStorage, ScriptedAiGateway]:
    admin = await create_user(conn)
    await conn.execute("update profiles set is_platform_admin=true where id=$1", admin)
    storage, ai = FakeStorage(), ScriptedAiGateway()
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
            files={"file": ("cp.pdf", source_pdf(text), "application/pdf")},
        )
        queued = response.json()
    extractor = ProcessReferenceHandler(
        uow_on(conn), storage, pdf or NationalReferencePdf(policy), ai, policy
    )
    await extractor.execute(UUID(queued["document_id"]), UUID(queued["job_id"]))
    return admin, UUID(queued["document_id"]), storage, ai


def drafter(
    conn: asyncpg.Connection, ai: ScriptedAiGateway, policy: ReferencePolicy = DRAFT_POLICY
) -> DraftReferenceHandler:
    return DraftReferenceHandler(uow_on(conn), ai, policy)


async def draft_job(conn: asyncpg.Connection, document_id: UUID) -> UUID | None:
    job_id: UUID | None = await conn.fetchval(
        "select draft_job_id from national_reference_documents where id=$1", document_id
    )
    return job_id


async def test_draft_columns_are_not_exposed_to_browser_roles(conn: asyncpg.Connection) -> None:
    for role in ("anon", "authenticated"):
        assert not await conn.fetchval(
            "select has_column_privilege($1, 'public.national_reference_documents', 'draft',"
            " 'select')",
            role,
        )
    assert await conn.fetchval("select 'cp_extract' = any(enum_range(null::ai_purpose)::text[])")


async def test_extraction_queues_one_draft_that_passes_review(conn: asyncpg.Connection) -> None:
    admin, document_id, storage, ai = await upload(conn)
    job_id = await draft_job(conn, document_id)
    assert job_id is not None
    ai.script("extract_curriculum", excerpt(["Pada akhir fase D, peserta didik menjelaskan gaya."]))
    await drafter(conn, ai).execute(document_id, job_id)
    row = await conn.fetchrow(
        "select draft_status,draft,review from national_reference_documents where id=$1",
        document_id,
    )
    assert row is not None
    draft = json.loads(row["draft"])
    assert row["draft_status"] == "ready" and row["review"] is None
    assert draft["curriculum"]["is_current"] is False
    assert (
        await conn.fetchval("select count(*) from ai_invocations where purpose='cp_extract'") == 1
    )
    review = draft | {"base_revision": 1}
    review["curriculum"] |= {"decree_code": str(uuid4()), "effective_on": "2025-07-16"}
    async with api_client(conn, storage=storage, ai=ai) as api:
        saved = await api.put(
            f"/platform/references/{document_id}/review", json=review, headers=as_user(admin)
        )
    assert saved.status_code == 200, saved.text


async def test_draft_never_touches_saved_review(conn: asyncpg.Connection) -> None:
    _, document_id, _, ai = await upload(conn)
    await conn.execute(
        "update national_reference_documents set review='{\"selected_pages\": []}'::jsonb"
        " where id=$1",
        document_id,
    )
    job_id = await draft_job(conn, document_id)
    assert job_id is not None
    ai.script("extract_curriculum", excerpt(["Peserta didik menjelaskan gerak."]))
    await drafter(conn, ai).execute(document_id, job_id)
    row = await conn.fetchrow(
        "select review,draft_status from national_reference_documents where id=$1", document_id
    )
    assert row is not None
    assert json.loads(row["review"]) == {"selected_pages": []} and row["draft_status"] == "ready"


async def test_stale_draft_job_cannot_write(conn: asyncpg.Connection) -> None:
    admin, document_id, storage, ai = await upload(conn)
    old_job = await draft_job(conn, document_id)
    assert old_job is not None
    async with api_client(conn, storage=storage, ai=ai, overrides=DRAFT_ENABLED) as api:
        await conn.execute(
            "update national_reference_documents set draft_status='failed' where id=$1",
            document_id,
        )
        response = await api.post(
            f"/platform/references/{document_id}/draft",
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202, response.text
    ai.script("extract_curriculum", excerpt(["Peserta didik menjelaskan gerak."]))
    await drafter(conn, ai).execute(document_id, old_job)
    assert ai.calls == []
    assert (
        await conn.fetchval(
            "select draft_status from national_reference_documents where id=$1", document_id
        )
        == "pending"
    )


async def test_draft_failure_records_provenance_and_keeps_review_open(
    conn: asyncpg.Connection,
) -> None:
    _, document_id, _, ai = await upload(conn)
    job_id = await draft_job(conn, document_id)
    assert job_id is not None
    ai.script(
        "extract_curriculum",
        AiServiceError("invalid_input", 422, [invocation("cp_extract", "error")]),
    )
    await drafter(conn, ai).execute(document_id, job_id)
    row = await conn.fetchrow(
        "select status,draft_status,draft_error from national_reference_documents where id=$1",
        document_id,
    )
    assert row is not None
    assert (row["status"], row["draft_status"], row["draft_error"]) == (
        "review",
        "failed",
        "invalid_input",
    )
    assert (
        await conn.fetchval(
            "select count(*) from ai_invocations where purpose='cp_extract' and status='failed'"
        )
        == 1
    )


async def test_document_without_cp_is_ready_without_ai_call(conn: asyncpg.Connection) -> None:
    _, document_id, _, ai = await upload(conn, "Kajian akademik tentang kurikulum.")
    job_id = await draft_job(conn, document_id)
    assert job_id is not None
    await drafter(conn, ai).execute(document_id, job_id)
    row = await conn.fetchrow(
        "select draft_status,draft,draft_report from national_reference_documents where id=$1",
        document_id,
    )
    assert row is not None
    assert row["draft_status"] == "ready"
    assert json.loads(row["draft"])["curriculum"]["subjects"] == []
    assert json.loads(row["draft_report"])["pages_considered"] == 0 and ai.calls == []


async def test_oversized_document_is_skipped_without_ai_call(conn: asyncpg.Connection) -> None:
    _, document_id, _, ai = await upload(conn)
    job_id = await draft_job(conn, document_id)
    assert job_id is not None
    tiny = replace(DRAFT_POLICY, draft_window_chars=10, draft_max_windows=0)
    await drafter(conn, ai, tiny).execute(document_id, job_id)
    row = await conn.fetchrow(
        "select draft_status,draft_error from national_reference_documents where id=$1",
        document_id,
    )
    assert row is not None
    assert (row["draft_status"], row["draft_error"]) == ("skipped", "REFERENCE_DRAFT_TOO_LARGE")
    assert ai.calls == []


async def test_guidance_documents_queue_no_draft(conn: asyncpg.Connection) -> None:
    _, document_id, _, _ = await upload(conn, "Buku IPA kelas VIII.", kind="guidance")
    assert await draft_job(conn, document_id) is None


async def test_disabled_flag_queues_no_draft(conn: asyncpg.Connection) -> None:
    _, document_id, _, _ = await upload(conn, policy=POLICY)
    assert await draft_job(conn, document_id) is None


async def test_draft_job_is_visible_only_to_the_requesting_platform_admin(
    conn: asyncpg.Connection,
) -> None:
    admin, document_id, storage, ai = await upload(conn)
    job_id = await draft_job(conn, document_id)
    other = await create_user(conn)
    await conn.execute("update profiles set is_platform_admin=true where id=$1", other)
    async with api_client(conn, storage=storage, ai=ai) as api:
        assert (await api.get(f"/jobs/{job_id}", headers=as_user(admin))).status_code == 200
        assert (await api.get(f"/jobs/{job_id}", headers=as_user(other))).status_code == 404


async def test_detail_shows_draft_and_report_to_platform_only(
    conn: asyncpg.Connection, world: World
) -> None:
    admin, document_id, storage, ai = await upload(conn)
    job_id = await draft_job(conn, document_id)
    assert job_id is not None
    ai.script(
        "extract_curriculum",
        excerpt(["Peserta didik menjelaskan gerak.", "Peserta didik menjelaskan api."]),
    )
    await drafter(conn, ai).execute(document_id, job_id)
    async with api_client(conn, storage=storage, ai=ai, overrides=DRAFT_ENABLED) as api:
        body = (await api.get(f"/platform/references/{document_id}", headers=as_user(admin))).json()
        assert body["draft_status"] == "ready"
        assert body["draft"]["curriculum"]["decree_code"] is None
        assert [r["reason"] for r in body["draft_report"]["rejected"]] == ["not_in_source"]
        for actor in (world.teacher_id, world.admin_id):
            assert (
                await api.get(f"/platform/references/{document_id}", headers=as_user(actor))
            ).status_code == 404
            assert (
                await api.post(
                    f"/platform/references/{document_id}/draft",
                    headers=as_user(actor) | {"Idempotency-Key": str(uuid4())},
                )
            ).status_code == 404


async def test_draft_request_is_idempotent_and_refused_while_pending(
    conn: asyncpg.Connection,
) -> None:
    admin, document_id, storage, ai = await upload(conn)
    async with api_client(conn, storage=storage, ai=ai, overrides=DRAFT_ENABLED) as api:
        url, key = f"/platform/references/{document_id}/draft", str(uuid4())
        refused = await api.post(url, headers=as_user(admin) | {"Idempotency-Key": key})
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "REFERENCE_DRAFT_UNAVAILABLE"
        await conn.execute(
            "update national_reference_documents set draft_status='failed' where id=$1",
            document_id,
        )
        key = str(uuid4())
        first = await api.post(url, headers=as_user(admin) | {"Idempotency-Key": key})
        again = await api.post(url, headers=as_user(admin) | {"Idempotency-Key": key})
        assert first.status_code == 202 and again.json() == first.json()


async def test_draft_request_is_refused_when_the_flag_is_off(conn: asyncpg.Connection) -> None:
    admin, document_id, storage, ai = await upload(conn)
    async with api_client(conn, storage=storage, ai=ai) as api:
        response = await api.post(
            f"/platform/references/{document_id}/draft",
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REFERENCE_DRAFT_DISABLED"


UNIQUE = ["satu", "dua", "tiga", "empat"]


class HeaderedPdf:
    async def extract(self, data: bytes) -> dict[int, str]:
        return {n: f"Judul Berulang\nIsi unik {UNIQUE[n - 1]}." for n in range(1, 5)}

    async def select(self, data: bytes, pages: list[int]) -> bytes:
        return data


async def stored_pages(conn: asyncpg.Connection, document_id: UUID) -> list[str]:
    rows = await conn.fetch(
        "select text from national_reference_pages where document_id=$1 order by page_number",
        document_id,
    )
    return [row["text"] for row in rows]


async def test_running_lines_are_stripped_from_curriculum_pages_when_the_flag_is_on(
    conn: asyncpg.Connection,
) -> None:
    _, document_id, _, _ = await upload(conn, pdf=HeaderedPdf())
    assert await stored_pages(conn, document_id) == [f"Isi unik {word}." for word in UNIQUE]


async def test_running_lines_are_kept_when_the_flag_is_off(conn: asyncpg.Connection) -> None:
    _, document_id, _, _ = await upload(conn, policy=POLICY, pdf=HeaderedPdf())
    assert (await stored_pages(conn, document_id))[0].startswith("Judul Berulang")


async def test_guidance_pages_keep_their_running_lines(conn: asyncpg.Connection) -> None:
    _, document_id, _, _ = await upload(conn, kind="guidance", pdf=HeaderedPdf())
    assert (await stored_pages(conn, document_id))[0].startswith("Judul Berulang")


async def test_publishing_supersedes_a_pending_draft_and_its_job_makes_no_ai_call(
    conn: asyncpg.Connection,
) -> None:
    admin, document_id, storage, ai = await upload(conn)
    old_job = await draft_job(conn, document_id)
    assert old_job is not None
    review = {
        "base_revision": 1,
        "curriculum": {
            "name": "CP IPA",
            "decree_code": str(uuid4()),
            "effective_on": "2025-07-16",
            "is_current": False,
            "subjects": [
                {
                    "name": "IPA",
                    "phase": "D",
                    "elements": [
                        {
                            "element": "Pemahaman IPA",
                            "description": CP_TEXT,
                            "page_start": 1,
                            "page_end": 1,
                            "statements": [
                                {
                                    "description": "Peserta didik menjelaskan gerak.",
                                    "page_start": 1,
                                    "page_end": 1,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "selected_pages": [],
    }
    async with api_client(conn, storage=storage, ai=ai) as api:
        saved = await api.put(
            f"/platform/references/{document_id}/review", json=review, headers=as_user(admin)
        )
        assert saved.status_code == 200, saved.text
        published = await api.post(
            f"/platform/references/{document_id}/publish",
            json={"base_revision": 2},
            headers=as_user(admin) | {"Idempotency-Key": str(uuid4())},
        )
        assert published.status_code == 202, published.text
    await drafter(conn, ai).execute(document_id, old_job)
    row = await conn.fetchrow(
        "select draft_status,draft_error from national_reference_documents where id=$1",
        document_id,
    )
    assert row is not None
    assert (row["draft_status"], row["draft_error"]) == ("failed", "REFERENCE_DRAFT_SUPERSEDED")
    assert ai.calls == []
    assert await conn.fetchval("select status from jobs where id=$1", old_job) == "succeeded"


async def test_unnamed_element_is_reported_and_the_detail_still_loads(
    conn: asyncpg.Connection,
) -> None:
    admin, document_id, storage, ai = await upload(conn)
    job_id = await draft_job(conn, document_id)
    assert job_id is not None
    ai.script("extract_curriculum", excerpt(["Peserta didik menjelaskan gerak."], element=" "))
    await drafter(conn, ai).execute(document_id, job_id)
    async with api_client(conn, storage=storage, ai=ai) as api:
        response = await api.get(f"/platform/references/{document_id}", headers=as_user(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"]["curriculum"]["subjects"] == []
    assert [r["reason"] for r in body["draft_report"]["rejected"]] == ["unnamed"]
