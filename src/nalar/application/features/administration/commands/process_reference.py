import hashlib
from collections.abc import Sequence
from functools import partial
from math import isfinite
from uuid import UUID, uuid4

from nalar.application.errors import Conflict
from nalar.application.features.administration.commands.upload_reference import (
    EXTRACT_REFERENCE,
    INDEX_REFERENCE,
    queue_draft,
)
from nalar.application.features.knowledge_base.s1_calls import BuildBusy, StepFailed, call_s1
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.ai_contract import EmbedIn, InvocationOut, Tag
from nalar.application.ports.national_references import ReferencePdf, ReferencePolicy, ReferenceRow
from nalar.application.ports.storage import MATERIALS_BUCKET, ObjectStorage
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.curriculum_draft import strip_running_lines


class ProcessReferenceHandler:
    def __init__(
        self,
        uow: UnitOfWork,
        storage: ObjectStorage,
        pdf: ReferencePdf,
        ai: AiGateway,
        policy: ReferencePolicy,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._pdf = pdf
        self._ai = ai
        self._policy = policy

    async def execute(self, document_id: UUID, job_id: UUID) -> None:
        token = uuid4()
        async with self._uow:
            job = await self._uow.jobs.get(job_id)
            if job is None or job.status in ("succeeded", "failed"):
                return
            if job.entity_id != document_id or job.kind not in (EXTRACT_REFERENCE, INDEX_REFERENCE):
                return
            row = await self._uow.national_references.get(document_id)
            if row is None or row["job_id"] != job_id:
                return
            if not await self._uow.national_references.claim(
                document_id, job_id, token, self._policy.lease_s
            ):
                raise BuildBusy()
            await self._uow.jobs.mark_running(job_id)
        try:
            await self._authorize(job.requested_by)
            if job.kind == EXTRACT_REFERENCE:
                data = await self._storage.download(MATERIALS_BUCKET, row["storage_path"])
                if hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise StepFailed("REFERENCE_SOURCE_CHANGED")
                try:
                    pages = await self._pdf.extract(data)
                    if self._policy.ai_draft and row["kind"] == "curriculum":
                        pages = strip_running_lines(pages, self._policy.running_line_share)
                except Exception as exc:
                    code = str(exc) if isinstance(exc, ValueError) else "REFERENCE_PDF_INVALID"
                    raise StepFailed(code) from exc
                async with self._uow:
                    await self._authorize_in_transaction(job.requested_by)
                    if not await self._uow.national_references.extracted(document_id, token, pages):
                        raise BuildBusy()
                    if self._policy.ai_draft and row["kind"] == "curriculum":
                        assert job.requested_by is not None
                        await queue_draft(self._uow, document_id, job.requested_by)
                    await self._uow.jobs.mark_succeeded(job_id)
            else:
                vectors = await self._vectors(row)
                async with self._uow:
                    await self._authorize_in_transaction(job.requested_by)
                    assert job.requested_by is not None
                    if not await self._uow.national_references.publish(
                        document_id,
                        token,
                        job.requested_by,
                        vectors,
                        self._policy.embedding_model,
                    ):
                        raise BuildBusy()
                    await self._uow.audit.record(
                        None,
                        job.requested_by,
                        "platform.publish_reference",
                        "national_reference_documents",
                        document_id,
                        {},
                    )
                    await self._uow.jobs.mark_succeeded(job_id)
        except Exception as exc:
            terminal = (
                isinstance(exc, (StepFailed, Conflict))
                or job.attempts + 1 >= self._policy.max_attempts
            )
            code = (
                exc.code
                if isinstance(exc, (StepFailed, Conflict))
                else "REFERENCE_PROCESSING_FAILED"
            )
            async with self._uow:
                changed = await self._uow.national_references.release(
                    document_id, token, code if terminal else None
                )
                if terminal and changed:
                    await self._uow.jobs.mark_failed(job_id, code, "Pemrosesan referensi gagal.")
            if not terminal:
                raise

    async def _authorize(self, actor_id: UUID | None) -> None:
        async with self._uow:
            await self._authorize_in_transaction(actor_id)

    async def _authorize_in_transaction(self, actor_id: UUID | None) -> None:
        if actor_id is None or not await self._uow.authz.is_platform_admin(actor_id):
            raise StepFailed("REFERENCE_ADMIN_REVOKED")

    async def _vectors(self, row: ReferenceRow) -> list[list[float]]:
        if row["kind"] != "curriculum":
            return []
        texts = [
            statement["description"]
            for subject in row["review"]["curriculum"]["subjects"]
            for element in subject["elements"]
            for statement in element["statements"]
        ]
        vectors: list[list[float]] = []

        async def record(invocations: Sequence[InvocationOut]) -> None:
            async with self._uow:
                await self._uow.ai_invocations.record(None, invocations)

        for offset in range(0, len(texts), 256):
            batch = texts[offset : offset + 256]
            body = EmbedIn(tag=Tag.cp_statement, texts=batch)
            reply = await call_s1(
                partial(self._ai.embed, body),
                f"reference-{row['id']}-{offset}-{uuid4()}",
                record,
            )
            result = reply.result
            if result.embedding_model != self._policy.embedding_model:
                raise StepFailed("EMBEDDING_MODEL_MISMATCH")
            if (
                result.dimensions != 1536
                or len(result.vectors) != len(batch)
                or any(len(v) != 1536 or not all(map(isfinite, v)) for v in result.vectors)
            ):
                raise StepFailed("EMBEDDING_INVALID")
            vectors.extend(result.vectors)
        return vectors
