from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import Forbidden, NotFound
from nalar.application.features.knowledge_base.commands.create_kb import (
    MaterialQueued,
    UploadedPdf,
    UploadLimits,
    check_pdf,
    new_material,
    queue_material,
)
from nalar.application.ports.storage import MATERIALS_BUCKET, ObjectStorage
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class AddMaterial:
    actor_id: UUID
    kb_id: UUID
    file: UploadedPdf


async def require_owner(uow: UnitOfWork, actor_id: UUID, kb_id: UUID) -> None:
    """Owner → ok; a teacher who can read the KB → 403 NOT_OWNER; anyone else → 404."""
    if await uow.authz.owns_kb(actor_id, kb_id):
        return
    if await uow.authz.can_read_kb(actor_id, kb_id):
        raise Forbidden("NOT_OWNER")
    raise NotFound()


class AddMaterialHandler:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage, limits: UploadLimits) -> None:
        self._uow = uow
        self._storage = storage
        self._limits = limits

    async def execute(self, cmd: AddMaterial) -> MaterialQueued:
        async with self._uow:
            await require_owner(self._uow, cmd.actor_id, cmd.kb_id)
            kb = await self._uow.knowledge.kb_ref(cmd.kb_id)
        assert kb is not None
        check_pdf(cmd.file, self._limits)
        material = new_material(kb, cmd.file)
        await self._storage.upload(
            MATERIALS_BUCKET, material.storage_path, cmd.file.data, "application/pdf"
        )
        async with self._uow:
            await require_owner(self._uow, cmd.actor_id, cmd.kb_id)
            job_id = await queue_material(self._uow, kb, cmd.actor_id, material)
        return MaterialQueued(kb.id, material.id, job_id)
