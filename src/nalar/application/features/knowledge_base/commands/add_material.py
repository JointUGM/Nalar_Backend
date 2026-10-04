from dataclasses import asdict, dataclass
from uuid import UUID

from nalar.application.errors import Forbidden, NotFound
from nalar.application.features.knowledge_base.commands.create_kb import (
    MaterialQueued,
    UploadedPdf,
    UploadLimits,
    check_pdf,
    new_material,
    queue_material,
    queued_receipt,
    upload_digest,
)
from nalar.application.ports.storage import MATERIALS_BUCKET, ObjectStorage
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class AddMaterial:
    actor_id: UUID
    kb_id: UUID
    file: UploadedPdf
    request_key: UUID | None = None


async def require_owner(uow: UnitOfWork, actor_id: UUID, kb_id: UUID) -> None:
    """Owner → ok; a teacher who can read the KB → 403 NOT_OWNER; anyone else → 404."""
    if await uow.authz.owns_kb(actor_id, kb_id):
        await uow.knowledge.require_active(kb_id)
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
            check_pdf(cmd.file, self._limits)
            request_id = None
            if cmd.request_key:
                request_id, _ = await self._uow.administration.request(
                    cmd.actor_id,
                    "material.upload",
                    cmd.kb_id,
                    cmd.request_key,
                    upload_digest(cmd.file),
                )
                receipt = await self._uow.administration.request_receipt(request_id)
                if receipt:
                    return queued_receipt(receipt)
            kb = await self._uow.knowledge.kb_ref(cmd.kb_id)
        assert kb is not None
        material = new_material(kb, cmd.file, request_id)
        await self._storage.upload(
            MATERIALS_BUCKET, material.storage_path, cmd.file.data, "application/pdf"
        )
        async with self._uow:
            await require_owner(self._uow, cmd.actor_id, cmd.kb_id)
            if cmd.request_key:
                assert request_id is not None
                await self._uow.administration.request(
                    cmd.actor_id,
                    "material.upload",
                    cmd.kb_id,
                    cmd.request_key,
                    upload_digest(cmd.file),
                )
                receipt = await self._uow.administration.request_receipt(request_id)
                if receipt:
                    return queued_receipt(receipt)
            job_id = await queue_material(self._uow, kb, cmd.actor_id, material)
            result = MaterialQueued(kb.id, material.id, job_id)
            if request_id:
                await self._uow.administration.finish_receipt(
                    request_id, material.id, asdict(result)
                )
        return result
