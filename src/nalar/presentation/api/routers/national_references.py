from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, File, Form, Header, Response, UploadFile
from pydantic import AnyHttpUrl

from nalar.application.features.administration.commands.request_draft import RequestDraftHandler
from nalar.application.features.administration.commands.review_reference import (
    PublishReferenceHandler,
    ReviewReferenceHandler,
)
from nalar.application.features.administration.commands.upload_reference import (
    UploadReferenceHandler,
)
from nalar.application.features.administration.queries.references import ReferencesQuery
from nalar.application.features.knowledge_base.commands.adopt_reference import AdoptReferenceHandler
from nalar.application.features.knowledge_base.commands.create_kb import UploadedPdf
from nalar.application.ports.national_references import ReferencePolicy
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.knowledge_base import MaterialQueuedOut
from nalar.presentation.api.schemas.national_references import (
    ReferenceDetailOut,
    ReferenceDraftQueuedOut,
    ReferenceQueuedOut,
    ReferenceReviewIn,
    ReferenceRevisionIn,
    ReferenceRevisionOut,
    ReferenceSummaryOut,
)

router = APIRouter(tags=["national-references"], route_class=DishkaRoute)
type RequestKey = Annotated[UUID, Header(alias="Idempotency-Key")]


@router.post("/platform/references", status_code=202, response_model=ReferenceQueuedOut)
async def upload(
    user: CurrentUser,
    handler: FromDishka[UploadReferenceHandler],
    policy: FromDishka[ReferencePolicy],
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["curriculum", "guidance"], Form()],
    title: Annotated[str, Form(min_length=1, max_length=200)],
    issuer: Annotated[str, Form(min_length=1, max_length=200)],
    source_url: Annotated[AnyHttpUrl, Form()],
    idempotency_key: RequestKey,
) -> ReferenceQueuedOut:
    data = await file.read(policy.max_bytes + 1)
    done = await handler.execute(
        user.id,
        UploadedPdf(file.filename or "reference.pdf", data),
        {
            "kind": kind,
            "title": title.strip(),
            "issuer": issuer.strip(),
            "source_url": str(source_url),
        },
        idempotency_key,
    )
    return ReferenceQueuedOut.model_validate(done)


@router.get("/platform/references", response_model=list[ReferenceSummaryOut])
async def list_platform(
    user: CurrentUser, query: FromDishka[ReferencesQuery]
) -> list[ReferenceSummaryOut]:
    return [ReferenceSummaryOut.model_validate(row) for row in await query.list(user.id)]


@router.get("/schools/{school_id}/national-references", response_model=list[ReferenceSummaryOut])
async def list_teacher(
    school_id: UUID,
    user: CurrentUser,
    query: FromDishka[ReferencesQuery],
) -> list[ReferenceSummaryOut]:
    return [ReferenceSummaryOut.model_validate(row) for row in await query.list(user.id, school_id)]


@router.get("/platform/references/{document_id}", response_model=ReferenceDetailOut)
async def detail(
    document_id: UUID,
    user: CurrentUser,
    query: FromDishka[ReferencesQuery],
) -> ReferenceDetailOut:
    return ReferenceDetailOut.model_validate(await query.get(user.id, document_id))


@router.get("/platform/references/{document_id}/file", response_class=Response)
async def source_file(
    document_id: UUID,
    user: CurrentUser,
    query: FromDishka[ReferencesQuery],
) -> Response:
    return Response(
        await query.file(user.id, document_id),
        media_type="application/pdf",
        headers={"Cache-Control": "private, no-store"},
    )


@router.put("/platform/references/{document_id}/review", response_model=ReferenceRevisionOut)
async def review(
    document_id: UUID,
    body: ReferenceReviewIn,
    user: CurrentUser,
    handler: FromDishka[ReviewReferenceHandler],
) -> ReferenceRevisionOut:
    revision = await handler.execute(
        user.id,
        document_id,
        body.base_revision,
        body.model_dump(mode="json", exclude={"base_revision"}),
    )
    return ReferenceRevisionOut(revision=revision)


@router.post(
    "/platform/references/{document_id}/publish", status_code=202, response_model=ReferenceQueuedOut
)
async def publish(
    document_id: UUID,
    body: ReferenceRevisionIn,
    user: CurrentUser,
    handler: FromDishka[PublishReferenceHandler],
    idempotency_key: RequestKey,
) -> ReferenceQueuedOut:
    return ReferenceQueuedOut.model_validate(
        await handler.execute(user.id, document_id, body.base_revision, idempotency_key)
    )


@router.post(
    "/platform/references/{document_id}/retry", status_code=202, response_model=ReferenceQueuedOut
)
async def retry(
    document_id: UUID,
    body: ReferenceRevisionIn,
    user: CurrentUser,
    handler: FromDishka[PublishReferenceHandler],
    idempotency_key: RequestKey,
) -> ReferenceQueuedOut:
    return ReferenceQueuedOut.model_validate(
        await handler.execute(user.id, document_id, body.base_revision, idempotency_key, retry=True)
    )


@router.post(
    "/platform/references/{document_id}/draft",
    status_code=202,
    response_model=ReferenceDraftQueuedOut,
)
async def request_draft(
    document_id: UUID,
    user: CurrentUser,
    handler: FromDishka[RequestDraftHandler],
    idempotency_key: RequestKey,
) -> ReferenceDraftQueuedOut:
    return ReferenceDraftQueuedOut.model_validate(
        await handler.execute(user.id, document_id, idempotency_key)
    )


@router.post(
    "/knowledge-bases/{kb_id}/national-references/{document_id}",
    status_code=202,
    response_model=MaterialQueuedOut,
)
async def adopt(
    kb_id: UUID,
    document_id: UUID,
    user: CurrentUser,
    handler: FromDishka[AdoptReferenceHandler],
    idempotency_key: RequestKey,
) -> MaterialQueuedOut:
    done = await handler.execute(user.id, kb_id, document_id, idempotency_key)
    return MaterialQueuedOut(**asdict(done))
