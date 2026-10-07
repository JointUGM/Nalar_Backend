from collections.abc import Sequence
from functools import partial
from typing import Any
from uuid import UUID, uuid4

from nalar.application.features.administration.commands.upload_reference import DRAFT_REFERENCE
from nalar.application.features.knowledge_base.s1_calls import StepFailed, call_s1
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.ai_contract import ExcerptPageIn, ExtractCurriculumIn, InvocationOut
from nalar.application.ports.national_references import ReferencePolicy
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.curriculum_draft import assemble_draft, candidate_pages, page_windows


class DraftReferenceHandler:
    def __init__(self, uow: UnitOfWork, ai: AiGateway, policy: ReferencePolicy) -> None:
        self._uow = uow
        self._ai = ai
        self._policy = policy

    async def execute(self, document_id: UUID, job_id: UUID) -> None:
        async with self._uow:
            job = await self._uow.jobs.get(job_id)
            if (
                job is None
                or job.status in ("succeeded", "failed")
                or job.kind != DRAFT_REFERENCE
                or job.entity_id != document_id
            ):
                return
            row = await self._uow.national_references.get(document_id)
            if row is None or row["draft_job_id"] != job_id or row["draft_status"] != "pending":
                await self._uow.jobs.mark_succeeded(job_id)
                return
            if row["status"] not in ("review", "failed"):
                await self._uow.jobs.mark_succeeded(job_id)
                return
            pages = await self._uow.national_references.pages(document_id)
            await self._uow.jobs.mark_running(job_id)
        try:
            if job.requested_by is None or not await self._is_platform_admin(job.requested_by):
                raise StepFailed("REFERENCE_ADMIN_REVOKED")
            numbers = candidate_pages(pages)
            windows = page_windows(numbers, pages, self._policy.draft_window_chars)
            if len(windows) > self._policy.draft_max_windows:
                await self._finish(document_id, job_id, "skipped", "REFERENCE_DRAFT_TOO_LARGE")
                return
            outputs: list[dict[str, Any]] = []
            # D-CPD-8: one unit of work cannot serve concurrent calls, so windows run one at a time.
            for index, window in enumerate(windows):
                body = ExtractCurriculumIn(
                    title=row["title"],
                    pages=[ExcerptPageIn(page_number=n, text=pages[n]) for n in window],
                )
                reply = await call_s1(
                    partial(self._ai.extract_curriculum, body),
                    f"reference-draft-{job_id}-{index}-{uuid4()}",
                    self._record,
                )
                outputs.append(reply.result.model_dump(mode="json"))
            draft, report = assemble_draft(
                row["title"], outputs, pages, self._policy.max_statements
            )
            report |= {"pages_considered": len(numbers), "windows": len(windows)}
            async with self._uow:
                await self._uow.national_references.save_draft(document_id, job_id, draft, report)
                await self._uow.jobs.mark_succeeded(job_id)
        except Exception as exc:
            terminal = isinstance(exc, StepFailed) or job.attempts + 1 >= self._policy.max_attempts
            if not terminal:
                raise
            code = exc.code if isinstance(exc, StepFailed) else "REFERENCE_PROCESSING_FAILED"
            await self._finish(document_id, job_id, "failed", code)

    async def _is_platform_admin(self, actor_id: UUID) -> bool:
        async with self._uow:
            return await self._uow.authz.is_platform_admin(actor_id)

    async def _record(self, invocations: Sequence[InvocationOut]) -> None:
        async with self._uow:
            await self._uow.ai_invocations.record(None, invocations)

    async def _finish(self, document_id: UUID, job_id: UUID, status: str, code: str) -> None:
        async with self._uow:
            await self._uow.national_references.draft_failed(document_id, job_id, status, code)
            await self._uow.jobs.mark_failed(job_id, code, "Draf CP otomatis tidak tersedia.")
