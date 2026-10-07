import hashlib
from uuid import UUID, uuid5

from nalar.application.errors import DependencyUnavailable, InvalidInput, NotFound
from nalar.application.features.knowledge_base.commands.create_kb import (
    UploadedPdf,
    UploadLimits,
    check_pdf,
)
from nalar.application.ports.national_references import ReferencePolicy, ReferenceRow
from nalar.application.ports.queue import KB_QUEUE
from nalar.application.ports.storage import MATERIALS_BUCKET, ObjectStorage
from nalar.application.ports.uow import UnitOfWork

EXTRACT_REFERENCE = "national_reference_extract"
INDEX_REFERENCE = "national_reference_index"
DRAFT_REFERENCE = "national_reference_draft"


async def require_platform(uow: UnitOfWork, actor_id: UUID) -> None:
    if not await uow.authz.is_platform_admin(actor_id):
        raise NotFound()


async def queue_reference(
    uow: UnitOfWork,
    document_id: UUID,
    actor_id: UUID,
    kind: str,
) -> UUID:
    job_id = await uow.jobs.create(
        kind=kind,
        entity_type="national_reference_documents",
        entity_id=document_id,
        school_id=None,
        requested_by=actor_id,
    )
    await uow.national_references.queue(
        document_id,
        job_id,
        "extracting" if kind == EXTRACT_REFERENCE else "indexing",
    )
    await uow.queue.send(
        KB_QUEUE, {"kind": kind, "document_id": str(document_id), "job_id": str(job_id)}
    )
    return job_id


async def queue_draft(uow: UnitOfWork, document_id: UUID, actor_id: UUID) -> UUID:
    job_id = await uow.jobs.create(
        kind=DRAFT_REFERENCE,
        entity_type="national_reference_documents",
        entity_id=document_id,
        school_id=None,
        requested_by=actor_id,
    )
    await uow.national_references.draft_queued(document_id, job_id)
    await uow.queue.send(
        KB_QUEUE, {"kind": DRAFT_REFERENCE, "document_id": str(document_id), "job_id": str(job_id)}
    )
    return job_id


class UploadReferenceHandler:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage, policy: ReferencePolicy) -> None:
        self._uow = uow
        self._storage = storage
        self._policy = policy

    async def execute(
        self,
        actor_id: UUID,
        file: UploadedPdf,
        details: ReferenceRow,
        key: UUID,
    ) -> ReferenceRow:
        async with self._uow:
            await require_platform(self._uow, actor_id)
            if not details["title"].strip() or not details["issuer"].strip():
                raise InvalidInput("REFERENCE_METADATA_REQUIRED")
            check_pdf(file, UploadLimits(self._policy.max_bytes))
            checksum = hashlib.sha256(file.data).hexdigest()
            digest = hashlib.sha256((str(sorted(details.items())) + checksum).encode()).hexdigest()
            request_id, _ = await self._uow.administration.request(
                actor_id,
                "reference.upload",
                actor_id,
                key,
                digest,
            )
            receipt = await self._uow.administration.request_receipt(request_id)
            if receipt:
                return receipt
            document_id = uuid5(request_id, "reference")
            path = f"platform/{document_id}/source.pdf"
            if await self._uow.national_references.get(document_id) is None:
                await self._uow.national_references.insert(
                    details
                    | {
                        "id": document_id,
                        "uploaded_by": actor_id,
                        "sha256": checksum,
                        "storage_path": path,
                    }
                )
        try:
            await self._storage.upload(MATERIALS_BUCKET, path, file.data, "application/pdf")
        except Exception as exc:
            raise DependencyUnavailable("REFERENCE_STORAGE_UNAVAILABLE") from exc
        async with self._uow:
            await require_platform(self._uow, actor_id)
            await self._uow.administration.request(
                actor_id, "reference.upload", actor_id, key, digest
            )
            receipt = await self._uow.administration.request_receipt(request_id)
            if receipt:
                return receipt
            job_id = await queue_reference(self._uow, document_id, actor_id, EXTRACT_REFERENCE)
            receipt = {
                "document_id": str(document_id),
                "job_id": str(job_id),
                "status": "extracting",
            }
            await self._uow.administration.finish_receipt(request_id, document_id, receipt)
            await self._uow.audit.record(
                None,
                actor_id,
                "platform.upload_reference",
                "national_reference_documents",
                document_id,
                {"sha256": checksum},
            )
        return receipt
