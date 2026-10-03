from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, File, Form, Query, UploadFile

from nalar.application.features.knowledge_base.commands.add_material import (
    AddMaterial,
    AddMaterialHandler,
)
from nalar.application.features.knowledge_base.commands.create_kb import (
    CreateKb,
    CreateKbHandler,
    UploadedPdf,
    UploadLimits,
)
from nalar.application.features.knowledge_base.commands.edit_item import EditItem, EditItemHandler
from nalar.application.features.knowledge_base.commands.request_build import (
    RequestBuild,
    RequestBuildHandler,
)
from nalar.application.features.knowledge_base.commands.review_item import (
    ReviewItem,
    ReviewItemHandler,
)
from nalar.application.features.knowledge_base.queries.get_kb import GetKbQuery
from nalar.application.features.knowledge_base.queries.list_kbs import ListKbs, ListKbsQuery
from nalar.application.features.knowledge_base.queries.list_sections import ListSectionsQuery
from nalar.application.features.knowledge_base.queries.review_queue import ReviewQueueQuery
from nalar.application.ports.knowledge import ItemPatch
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.knowledge_base import (
    BuildQueuedOut,
    ConceptOut,
    ConceptPatchIn,
    KbDetailOut,
    KbPageOut,
    KbSummaryOut,
    MaterialOut,
    MaterialQueuedOut,
    MisconceptionOut,
    MisconceptionPatchIn,
    PrerequisiteOut,
    ReviewIn,
    ReviewOut,
    ReviewQueueOut,
    SectionItemOut,
    SectionsOut,
)

router = APIRouter(tags=["knowledge-base"], route_class=DishkaRoute)


async def _read(file: UploadFile, limits: UploadLimits) -> UploadedPdf:
    # One byte past the limit is enough to reject without buffering a larger upload.
    return UploadedPdf(file.filename or "materi.pdf", await file.read(limits.max_bytes + 1))


@router.post(
    "/schools/{school_id}/knowledge-bases", status_code=202, response_model=MaterialQueuedOut
)
async def create_kb(
    school_id: UUID,
    user: CurrentUser,
    school_subject_id: Annotated[UUID, Form()],
    topic_title: Annotated[str, Form(min_length=1, max_length=120)],
    file: Annotated[UploadFile, File()],
    handler: FromDishka[CreateKbHandler],
    limits: FromDishka[UploadLimits],
) -> MaterialQueuedOut:
    done = await handler.execute(
        CreateKb(user.id, school_id, school_subject_id, topic_title, await _read(file, limits))
    )
    return MaterialQueuedOut(**asdict(done))


@router.post(
    "/knowledge-bases/{kb_id}/materials", status_code=202, response_model=MaterialQueuedOut
)
async def add_material(
    kb_id: UUID,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    handler: FromDishka[AddMaterialHandler],
    limits: FromDishka[UploadLimits],
) -> MaterialQueuedOut:
    done = await handler.execute(AddMaterial(user.id, kb_id, await _read(file, limits)))
    return MaterialQueuedOut(**asdict(done))


@router.get("/schools/{school_id}/knowledge-bases", response_model=KbPageOut)
async def list_kbs(
    school_id: UUID,
    user: CurrentUser,
    query: FromDishka[ListKbsQuery],
    school_subject_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> KbPageOut:
    page = await query.execute(ListKbs(user.id, school_id, school_subject_id, limit, cursor))
    return KbPageOut(
        items=[
            KbSummaryOut(
                id=kb.id,
                topic_key=kb.topic_key,
                topic_title=kb.topic_title,
                school_subject_id=kb.school_subject_id,
                owner_teacher_id=kb.owner_teacher_id,
                owner_name=kb.owner_name,
                material_count=kb.material_count,
                built_section_count=kb.built_section_count,
                pending_count=kb.pending_count,
                approved_concept_count=kb.approved_concept_count,
                can_edit=kb.owner_teacher_id == user.id,
            )
            for kb in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/knowledge-bases/{kb_id}", response_model=KbDetailOut)
async def get_kb(kb_id: UUID, user: CurrentUser, query: FromDishka[GetKbQuery]) -> KbDetailOut:
    kb, can_edit = await query.execute(user.id, kb_id)
    return KbDetailOut(
        id=kb.id,
        topic_title=kb.topic_title,
        owner_teacher_id=kb.owner_teacher_id,
        materials=[MaterialOut(**asdict(m)) for m in kb.materials],
        concepts=[ConceptOut(**asdict(c)) for c in kb.concepts],
        prerequisites=[
            PrerequisiteOut(concept_id=a, prerequisite_concept_id=b) for a, b in kb.prerequisites
        ],
        misconceptions=[MisconceptionOut(**asdict(m)) for m in kb.misconceptions],
        can_edit=can_edit,
    )


@router.get("/knowledge-bases/{kb_id}/sections", response_model=SectionsOut)
async def list_sections(
    kb_id: UUID, user: CurrentUser, query: FromDishka[ListSectionsQuery]
) -> SectionsOut:
    sections = await query.execute(user.id, kb_id)
    return SectionsOut(items=[SectionItemOut(**asdict(s)) for s in sections])


@router.post(
    "/knowledge-bases/{kb_id}/sections/{section_id}/build",
    status_code=202,
    response_model=BuildQueuedOut,
)
async def build_section(
    kb_id: UUID, section_id: UUID, user: CurrentUser, handler: FromDishka[RequestBuildHandler]
) -> BuildQueuedOut:
    done = await handler.execute(RequestBuild(user.id, kb_id, section_id))
    return BuildQueuedOut(job_id=done.job_id, section_id=done.section_id)


@router.post("/concepts/{item_id}/review", response_model=ReviewOut)
async def review_concept(
    item_id: UUID, body: ReviewIn, user: CurrentUser, handler: FromDishka[ReviewItemHandler]
) -> ReviewOut:
    done = await handler.execute(ReviewItem(user.id, "concept", item_id, body.review_status))
    return ReviewOut(**asdict(done))


@router.post("/misconceptions/{item_id}/review", response_model=ReviewOut)
async def review_misconception(
    item_id: UUID, body: ReviewIn, user: CurrentUser, handler: FromDishka[ReviewItemHandler]
) -> ReviewOut:
    done = await handler.execute(ReviewItem(user.id, "misconception", item_id, body.review_status))
    return ReviewOut(**asdict(done))


@router.patch("/concepts/{item_id}", response_model=ConceptOut)
async def patch_concept(
    item_id: UUID, body: ConceptPatchIn, user: CurrentUser, handler: FromDishka[EditItemHandler]
) -> ConceptOut:
    view = await handler.execute(
        EditItem(
            user.id, "concept", item_id, ItemPatch(name=body.name, description=body.description)
        )
    )
    return ConceptOut(**asdict(view))


@router.patch("/misconceptions/{item_id}", response_model=MisconceptionOut)
async def patch_misconception(
    item_id: UUID,
    body: MisconceptionPatchIn,
    user: CurrentUser,
    handler: FromDishka[EditItemHandler],
) -> MisconceptionOut:
    patch = ItemPatch(
        statement=body.statement,
        correct_understanding=body.correct_understanding,
        detection_cues=tuple(body.detection_cues) if body.detection_cues is not None else None,
        counter_examples=tuple(body.counter_examples)
        if body.counter_examples is not None
        else None,
    )
    view = await handler.execute(EditItem(user.id, "misconception", item_id, patch))
    return MisconceptionOut(**asdict(view))


@router.get("/knowledge-bases/{kb_id}/review-queue", response_model=ReviewQueueOut)
async def review_queue(
    kb_id: UUID, user: CurrentUser, query: FromDishka[ReviewQueueQuery]
) -> ReviewQueueOut:
    concepts, misconceptions = await query.execute(user.id, kb_id)
    return ReviewQueueOut(
        knowledge_base_id=kb_id, pending_concepts=concepts, pending_misconceptions=misconceptions
    )
