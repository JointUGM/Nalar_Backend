import hashlib
from uuid import UUID, uuid5

from nalar.application.errors import Conflict, NotFound
from nalar.application.features.administration.commands.request_digest import request_digest
from nalar.application.features.knowledge_base.commands.add_material import require_owner
from nalar.application.features.knowledge_base.commands.create_kb import (
    MaterialQueued,
    UploadedPdf,
    UploadLimits,
    check_pdf,
    queue_material,
    queued_receipt,
)
from nalar.application.ports.knowledge import NewMaterial
from nalar.application.ports.national_references import ReferencePdf, ReferencePolicy
from nalar.application.ports.storage import MATERIALS_BUCKET, ObjectStorage
from nalar.application.ports.uow import UnitOfWork


class AdoptReferenceHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        storage: ObjectStorage,
        pdf: ReferencePdf,
        policy: ReferencePolicy,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._pdf = pdf
        self._policy = policy

    async def execute(
        self,
        actor_id: UUID,
        kb_id: UUID,
        document_id: UUID,
        key: UUID,
    ) -> MaterialQueued:
        async with self._uow:
            await require_owner(self._uow, actor_id, kb_id)
            row = await self._uow.national_references.get(document_id)
            if row is None or row["status"] != "published" or row["kind"] != "guidance":
                raise NotFound()
            request_id, _ = await self._uow.administration.request(
                actor_id,
                "reference.adopt",
                kb_id,
                key,
                request_digest(str(document_id)),
            )
            receipt = await self._uow.administration.request_receipt(request_id)
            if receipt:
                return queued_receipt(receipt)
            existing = await self._uow.national_references.adoption(kb_id, document_id)
            if existing:
                return queued_receipt(existing)
            kb = await self._uow.knowledge.kb_ref(kb_id)
            if kb is None:
                raise NotFound()
            pages = row["review"]["selected_pages"]
        original = await self._storage.download(MATERIALS_BUCKET, row["storage_path"])
        if hashlib.sha256(original).hexdigest() != row["sha256"]:
            raise Conflict("REFERENCE_SOURCE_CHANGED")
        selected = await self._pdf.select(original, pages)
        file = UploadedPdf(row["title"] + ".pdf", selected)
        check_pdf(file, UploadLimits(self._policy.max_bytes))
        material_id = uuid5(kb_id, str(document_id))
        material = NewMaterial(
            material_id, file.filename, f"{kb.school_id}/{kb.id}/{material_id}.pdf", len(selected)
        )
        await self._storage.upload(
            MATERIALS_BUCKET, material.storage_path, selected, "application/pdf"
        )
        async with self._uow:
            await require_owner(self._uow, actor_id, kb_id)
            current = await self._uow.national_references.get(document_id)
            if current is None or current["status"] != "published":
                raise Conflict("REFERENCE_UNAVAILABLE")
            existing = await self._uow.national_references.adoption(kb_id, document_id)
            if existing:
                return queued_receipt(existing)
            job_id = await queue_material(self._uow, kb, actor_id, material)
            await self._uow.national_references.attach(material_id, document_id, pages)
            receipt = {
                "knowledge_base_id": str(kb_id),
                "material_id": str(material_id),
                "job_id": str(job_id),
            }
            await self._uow.administration.finish_receipt(request_id, material_id, receipt)
            await self._uow.audit.record(
                kb.school_id,
                actor_id,
                "teacher.adopt_reference",
                "teaching_materials",
                material_id,
                {"reference_id": str(document_id), "source_pages": pages},
            )
        return MaterialQueued(kb_id, material_id, job_id)
