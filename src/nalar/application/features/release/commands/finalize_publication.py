import asyncio
import logging
from collections import defaultdict
from itertools import batched
from uuid import UUID

from nalar.application.features.integrity.commands.compute_publication_similarity import (
    ComputePublicationSimilarityHandler,
)
from nalar.application.features.release.insight_request import class_insight_body
from nalar.application.ports.ai import AiGateway, AiResult, AiServiceError
from nalar.application.ports.ai_contract import ParentSummaryIn, ParentSummaryOut
from nalar.application.ports.release import PublicationText, SummaryInput
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.class_map import aggregate
from nalar.domain.placeholders import (
    NarrativeLexicon,
    PlaceholderError,
    counts_from_snapshot,
    fill,
    quantity_problems,
)

log = logging.getLogger(__name__)
# INTEGRATION.md, S5: fan summaries out at 8 or fewer concurrent calls.
_SUMMARY_FANOUT = 8
_MAX_INSIGHT_TRIES = 2

type SummaryReply = AiResult[ParentSummaryOut] | AiServiceError


class FinalizePublicationHandler:
    """Master plan §6.3 tail: similarity, parent summaries, class insight. Each step skips work
    already stored, so the tick re-runs it until every piece exists (D-S26-12)."""

    def __init__(
        self,
        uow: UnitOfWork,
        ai: AiGateway,
        similarity: ComputePublicationSimilarityHandler,
        lexicon: NarrativeLexicon,
    ) -> None:
        self._uow = uow
        self._ai = ai
        self._similarity = similarity
        self._lexicon = lexicon

    async def execute(self, publication_id: UUID, job_id: UUID) -> None:
        async with self._uow:
            if not await self._uow.jobs.mark_running(job_id):
                return
            pub = await self._uow.release.publication_text(publication_id)
            runs = await self._uow.release.finalize_runs(publication_id)
        if pub is not None:
            await self._similarity.execute(publication_id)
            if pub.released_at is None:
                await self._summaries(pub)
            if runs < _MAX_INSIGHT_TRIES:
                await self._class_insight(pub)
        async with self._uow:
            await self._uow.jobs.mark_succeeded(job_id)

    async def _summary(self, pub: PublicationText, s: SummaryInput) -> SummaryReply:
        body = ParentSummaryIn.model_validate(
            {
                "mission_title": pub.mission_title,
                "concepts": [
                    {
                        "name": c.name,
                        "outcome": c.outcome,
                        "misconception_statement": c.misconception_statement,
                        "resolved_in_session": c.resolved_in_session,
                    }
                    for c in s.concepts
                ],
                "evaluation_summary": s.evaluation_summary,
            }
        )
        try:
            return await self._ai.parent_summary(body, f"summary-{pub.id}-{s.student_id}")
        except AiServiceError as error:
            return error

    async def _summaries(self, pub: PublicationText) -> None:
        async with self._uow:
            students = [s for s in await self._uow.release.summary_input(pub.id) if s.concepts]
        # Calls run 8 at a time; each batch is written in one transaction afterwards, because a
        # unit of work can't be shared by concurrent tasks.
        for batch in batched(students, _SUMMARY_FANOUT):
            replies = await asyncio.gather(*(self._summary(pub, s) for s in batch))
            async with self._uow:
                for s, reply in zip(batch, replies, strict=True):
                    if isinstance(reply, AiServiceError):
                        await self._uow.ai_invocations.record(pub.school_id, reply.invocations)
                        log.warning(
                            "parent summary failed: %s %s",
                            reply.code,
                            reply.http_status,
                            extra={"publication_id": str(pub.id)},
                        )
                        continue
                    ids = await self._uow.ai_invocations.record(pub.school_id, reply.invocations)
                    text = reply.result.content.strip()
                    problems = quantity_problems(text, self._lexicon, parent=True)
                    if problems or not text:
                        log.warning(
                            "parent summary rejected: %s",
                            problems,
                            extra={"publication_id": str(pub.id)},
                        )
                        continue
                    model = reply.result.source.value == "model"
                    await self._uow.release.insert_summary(
                        pub.school_id,
                        pub.id,
                        s.student_id,
                        text,
                        ids[-1] if model and ids else None,
                    )

    async def _class_insight(self, pub: PublicationText) -> None:
        async with self._uow:
            if await self._uow.release.has_insight(pub.id):
                return
            data = await self._uow.results.class_map_input(pub.id)
        if data is None:
            return
        by_concept: defaultdict[UUID, list[UUID]] = defaultdict(list)
        for misconception_id, concept_id, _ in data.misconceptions:
            by_concept[concept_id].append(misconception_id)
        body = class_insight_body(
            pub.mission_title,
            data,
            aggregate([c for c, _ in data.concepts], by_concept, data.attempts),
        )
        if body is None:
            return
        try:
            reply = await self._ai.class_insight(body, f"insight-{pub.id}")
        except AiServiceError as error:
            async with self._uow:
                await self._uow.ai_invocations.record(pub.school_id, error.invocations)
            log.warning(
                "class insight failed: %s %s",
                error.code,
                error.http_status,
                extra={"publication_id": str(pub.id)},
            )
            return
        snapshot = body.model_dump(mode="json")
        counts = counts_from_snapshot(snapshot)
        clusters = [c.model_dump(mode="json") for c in reply.result.clusters]
        async with self._uow:
            ids = await self._uow.ai_invocations.record(pub.school_id, reply.invocations)
            try:
                fill(reply.result.narrative, counts, self._lexicon)
                for cluster in clusters:
                    fill(cluster["explanation"], counts, self._lexicon)
            except PlaceholderError as error:
                log.warning(
                    "class insight rejected: %s", error, extra={"publication_id": str(pub.id)}
                )
                return
            await self._uow.release.insert_insight(
                pub.school_id,
                pub.id,
                snapshot,
                clusters,
                reply.result.narrative,
                ids[-1] if ids else None,
            )
