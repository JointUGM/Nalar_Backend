"""TC-23: the teacher asks S5 for one next-lesson activity; the worker drafts and stores it once.

Viewing never calls the model. Grouping, names and counts stay deterministic in the class map;
S5 only sees reviewed content for one misconception.
"""

import logging
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.ai import AiGateway, AiServiceError
from nalar.application.ports.ai_contract import TeachingPlanIn
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.results import TeachingPlanSource
from nalar.application.ports.uow import UnitOfWork

TEACHING_PLAN_KIND = "teaching_plan"

log = logging.getLogger(__name__)


def plan_body(source: TeachingPlanSource, misconception_id: UUID) -> TeachingPlanIn:
    """Clipped to the AI contract's limits so teacher text never fails validation."""
    return TeachingPlanIn.model_validate(
        {
            "mission_title": source.mission_title[:300],
            "learning_objective": source.learning_objective[:2000] or source.mission_title[:300],
            "concept_name": source.concept_name[:300],
            "misconception_id": misconception_id,
            "statement": source.statement[:1000],
            "correct_understanding": source.correct_understanding[:2000],
            "counter_examples": [e[:1000] for e in source.counter_examples[:5]],
            "detection_cues": [c[:1000] for c in source.detection_cues[:5]],
            "questions": [{"id": i[:64], "text": t[:1000]} for i, t in source.questions[:8] if t],
        }
    )


class RequestTeachingPlanHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, actor_id: UUID, publication_id: UUID, misconception_id: UUID
    ) -> UUID | None:
        """The queued job, or None when the plan already exists."""
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            source = await self._uow.results.teaching_plan_source(publication_id, misconception_id)
            if source is None:
                raise NotFound()
            if await self._uow.results.has_teaching_plan(publication_id, misconception_id):
                return None
            existing = await self._uow.results.active_teaching_plan_job(
                publication_id, misconception_id
            )
            if existing is not None:
                return existing
            job_id = await self._uow.jobs.create(
                kind=TEACHING_PLAN_KIND,
                entity_type="publications",
                entity_id=publication_id,
                school_id=source.school_id,
                requested_by=actor_id,
                result={"misconception_id": str(misconception_id)},
            )
            await self._uow.queue.send(
                DEFAULT_QUEUE,
                {
                    "kind": TEACHING_PLAN_KIND,
                    "job_id": str(job_id),
                    "publication_id": str(publication_id),
                    "misconception_id": str(misconception_id),
                },
            )
        return job_id


class BuildTeachingPlanHandler:
    def __init__(self, uow: UnitOfWork, ai: AiGateway) -> None:
        self._uow = uow
        self._ai = ai

    async def execute(self, job_id: UUID, publication_id: UUID, misconception_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.jobs.mark_running(job_id):
                return
            source = await self._uow.results.teaching_plan_source(publication_id, misconception_id)
            done = await self._uow.results.has_teaching_plan(publication_id, misconception_id)
            if source is None or done:
                await self._uow.jobs.mark_succeeded(job_id)
                return
        try:
            reply = await self._ai.teaching_plan(
                plan_body(source, misconception_id), f"plan-{job_id}"
            )
        except AiServiceError as error:
            async with self._uow:
                await self._uow.ai_invocations.record(source.school_id, error.invocations)
                await self._uow.jobs.mark_failed(
                    job_id, error.code, "Nala belum bisa menyusun kegiatan. Coba lagi nanti."
                )
            log.warning(
                "teaching plan failed: %s %s",
                error.code,
                error.http_status,
                extra={"publication_id": str(publication_id)},
            )
            return
        async with self._uow:
            ids = await self._uow.ai_invocations.record(source.school_id, reply.invocations)
            await self._uow.results.insert_teaching_plan(
                source.school_id,
                publication_id,
                misconception_id,
                reply.result.model_dump(mode="json"),
                ids[-1] if ids else None,
            )
            await self._uow.jobs.mark_succeeded(job_id)
