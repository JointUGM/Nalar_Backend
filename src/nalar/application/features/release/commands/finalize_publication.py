import logging
from collections import defaultdict
from uuid import UUID

from nalar.application.features.integrity.commands.compute_publication_similarity import (
    ComputePublicationSimilarityHandler,
)
from nalar.application.features.release.insight_request import class_insight_body
from nalar.application.ports.ai import AiGateway, AiServiceError
from nalar.application.ports.release import PublicationText
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.class_map import aggregate
from nalar.domain.placeholders import (
    NarrativeLexicon,
    PlaceholderError,
    counts_from_snapshot,
    fill,
)

log = logging.getLogger(__name__)
_MAX_INSIGHT_TRIES = 2


class FinalizePublicationHandler:
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
            if runs < _MAX_INSIGHT_TRIES:
                await self._class_insight(pub)
        async with self._uow:
            await self._uow.jobs.mark_succeeded(job_id)

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
                for suggestion in reply.result.suggestions:
                    fill(suggestion, counts, self._lexicon)
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
                reply.result.suggestions,
            )
