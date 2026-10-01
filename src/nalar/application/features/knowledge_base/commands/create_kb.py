from dataclasses import dataclass
from uuid import UUID, uuid4

from nalar.application.errors import Conflict, Forbidden, InvalidInput, NotFound
from nalar.application.features.knowledge_base.messages import detect_message
from nalar.application.ports.knowledge import KbRef, NewMaterial
from nalar.application.ports.queue import KB_QUEUE
from nalar.application.ports.storage import MATERIALS_BUCKET, ObjectStorage
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.topic_key import topic_key

_PDF_MAGIC = b"%PDF-"


@dataclass(frozen=True)
class UploadLimits:
    max_bytes: int


@dataclass(frozen=True)
class UploadedPdf:
    filename: str
    data: bytes


@dataclass(frozen=True)
class CreateKb:
    actor_id: UUID
    school_id: UUID
    school_subject_id: UUID
    topic_title: str
    file: UploadedPdf


@dataclass(frozen=True)
class MaterialQueued:
    knowledge_base_id: UUID
    material_id: UUID
    job_id: UUID


def check_pdf(file: UploadedPdf, limits: UploadLimits) -> None:
    if len(file.data) > limits.max_bytes:
        raise InvalidInput("FILE_TOO_LARGE", "Berkas lebih dari 50 MB.")
    if not file.data.startswith(_PDF_MAGIC):
        raise InvalidInput("FILE_NOT_PDF", "Hanya berkas PDF yang bisa diunggah.")


def new_material(kb: KbRef, file: UploadedPdf) -> NewMaterial:
    material_id = uuid4()
    return NewMaterial(
        material_id, file.filename, f"{kb.school_id}/{kb.id}/{material_id}.pdf", len(file.data)
    )


async def queue_material(uow: UnitOfWork, kb: KbRef, actor_id: UUID, material: NewMaterial) -> UUID:
    await uow.knowledge.add_material(kb, actor_id, material)
    job_id = await uow.jobs.create(
        kind="kb_detect_sections",
        entity_type="teaching_materials",
        entity_id=material.id,
        school_id=kb.school_id,
        requested_by=actor_id,
    )
    await uow.queue.send(KB_QUEUE, detect_message(material.id, job_id))
    return job_id


class CreateKbHandler:
    """TC-1: the caller becomes the owner; the PDF is stored before any row points at it."""

    def __init__(self, uow: UnitOfWork, storage: ObjectStorage, limits: UploadLimits) -> None:
        self._uow = uow
        self._storage = storage
        self._limits = limits

    async def execute(self, cmd: CreateKb) -> MaterialQueued:
        async with self._uow:
            if not await self._uow.authz.is_school_teacher(cmd.actor_id, cmd.school_id):
                raise NotFound()
            if not await self._uow.authz.teaches_subject(
                cmd.actor_id, cmd.school_id, cmd.school_subject_id
            ):
                raise Forbidden("NOT_ASSIGNED_TO_SUBJECT")
            check_pdf(cmd.file, self._limits)
            try:
                key = topic_key(cmd.topic_title)
            except ValueError as exc:
                raise InvalidInput(details={"topic_title": "invalid"}) from exc
            if await self._uow.knowledge.topic_exists(cmd.school_subject_id, key):
                raise Conflict("TOPIC_ALREADY_EXISTS")
        kb = KbRef(uuid4(), cmd.school_id, cmd.school_subject_id, cmd.actor_id)
        material = new_material(kb, cmd.file)
        # ponytail: a failed insert below leaves an orphan object; add a bucket sweep if it matters.
        await self._storage.upload(
            MATERIALS_BUCKET, material.storage_path, cmd.file.data, "application/pdf"
        )
        async with self._uow:
            if not await self._uow.knowledge.create_kb(kb, key, cmd.topic_title.strip()):
                raise Conflict("TOPIC_ALREADY_EXISTS")
            job_id = await queue_material(self._uow, kb, cmd.actor_id, material)
        return MaterialQueued(kb.id, material.id, job_id)
