from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.features.administration.commands.request_digest import request_digest
from nalar.application.features.administration.commands.upload_reference import (
    queue_draft,
    require_platform,
)
from nalar.application.ports.national_references import ReferencePolicy
from nalar.application.ports.uow import UnitOfWork


class RequestDraftHandler:
    def __init__(self, uow: UnitOfWork, policy: ReferencePolicy) -> None:
        self._uow = uow
        self._policy = policy

    async def execute(self, actor_id: UUID, document_id: UUID, key: UUID) -> dict[str, str]:
        async with self._uow:
            await require_platform(self._uow, actor_id)
            row = await self._uow.national_references.get(document_id, lock=True)
            if row is None:
                raise NotFound()
            request_id, _ = await self._uow.administration.request(
                actor_id, "reference.draft", document_id, key, request_digest("draft")
            )
            receipt = await self._uow.administration.request_receipt(request_id)
            if receipt:
                return receipt
            if not self._policy.ai_draft:
                raise Conflict("REFERENCE_DRAFT_DISABLED")
            if (
                row["kind"] != "curriculum"
                or row["status"] != "review"
                or row["draft_status"] == "pending"
            ):
                raise Conflict("REFERENCE_DRAFT_UNAVAILABLE")
            job_id = await queue_draft(self._uow, document_id, actor_id)
            receipt = {"document_id": str(document_id), "job_id": str(job_id)}
            await self._uow.administration.finish_receipt(request_id, document_id, receipt)
        return receipt
