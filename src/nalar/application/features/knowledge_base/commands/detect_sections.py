from collections.abc import Sequence
from uuid import UUID

from nalar.application.features.knowledge_base.s1_calls import StepFailed, call_s1
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.ai_contract import InvocationOut
from nalar.application.ports.storage import ObjectStorage
from nalar.application.ports.uow import UnitOfWork


class DetectSectionsHandler:
    """INTEGRATION.md step 1. A redelivered message for a finished job does nothing."""

    def __init__(self, uow: UnitOfWork, ai: AiGateway, storage: ObjectStorage) -> None:
        self._uow = uow
        self._ai = ai
        self._storage = storage

    async def execute(self, material_id: UUID, job_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.jobs.mark_running(job_id):
                return
            material = await self._uow.knowledge.material_for_detect(material_id)
            if material is None or await self._uow.knowledge.has_sections(material_id):
                await self._uow.jobs.mark_succeeded(job_id)
                return
        pdf = await self._storage.download(material.storage_bucket, material.storage_path)

        async def record(invocations: Sequence[InvocationOut]) -> None:
            async with self._uow:
                await self._uow.ai_invocations.record(material.school_id, invocations)

        try:
            reply = await call_s1(
                lambda rid: self._ai.detect_sections(pdf, material.title, rid),
                f"detect-{material_id}",
                record,
            )
        except StepFailed as failed:
            async with self._uow:
                await self._uow.knowledge.mark_material_failed(material_id)
                await self._uow.jobs.mark_failed(job_id, failed.code, "section detection failed")
            return
        result = reply.result
        async with self._uow:
            await self._uow.knowledge.store_detection(
                material, result.page_count, result.pages_without_text, result.sections
            )
            await self._uow.jobs.mark_succeeded(job_id)
