from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from itertools import batched
from uuid import UUID

from nalar.application.features.knowledge_base.s1_calls import (
    BuildSuperseded,
    StepFailed,
    call_s1,
)
from nalar.application.ports.ai import AiGateway, ChunkSpec
from nalar.application.ports.ai_contract import (
    AlignCpIn,
    AlignItemIn,
    ChunkKind,
    ChunkRefIn,
    ConceptForMisconceptionsIn,
    DedupeIn,
    DedupItemIn,
    ExistingConceptIn,
    ExtractConceptsIn,
    GenerateMisconceptionsIn,
    InvocationOut,
    PrerequisiteEdgeIn,
    RejectedConceptIn,
)
from nalar.application.ports.clock import Clock
from nalar.application.ports.knowledge import BuildContext, ChunkRow
from nalar.application.ports.storage import ObjectStorage
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class S1Settings:
    embedding_model: str
    default_phase: str
    stale_after_s: float


def _chunk_ref(c: ChunkRow) -> ChunkRefIn:
    return ChunkRefIn.model_validate(
        {
            "id": c.id,
            "content": c.content,
            "heading_path": c.heading_path or "",
            "kind": ChunkKind(c.kind),
            "page_start": c.page_start,
            "page_end": c.page_end,
        }
    )


class BuildSectionHandler:
    """Resumes committed S1 steps under a fenced job lease (D-S26-10)."""

    def __init__(
        self,
        uow: UnitOfWork,
        ai: AiGateway,
        storage: ObjectStorage,
        clock: Clock,
        settings: S1Settings,
    ) -> None:
        self._uow = uow
        self._ai = ai
        self._storage = storage
        self._clock = clock
        self._settings = settings

    async def execute(self, section_id: UUID, job_id: UUID) -> None:
        async with self._uow:
            job = await self._uow.jobs.get(job_id)
            if job is None or job.status not in ("queued", "running"):
                return
            ctx = await self._uow.knowledge.build_context(section_id, self._settings.default_phase)
            if ctx is None:
                await self._uow.jobs.mark_failed(job_id, "SECTION_NOT_FOUND", "")
                return
            attempt = await self._uow.knowledge.claim_build(
                ctx, job_id, self._clock.now(), self._settings.stale_after_s
            )
            if attempt is None:
                return
            self._job_id = job_id
            self._attempt = attempt
            # The KB lock may have waited for another worker's checkpoint commit.
            ctx = await self._uow.knowledge.build_context(section_id, self._settings.default_phase)
            assert ctx is not None
            await self._uow.knowledge.set_build_status(section_id, "building", None)
        try:
            chunks = await self._chunks(ctx)
            await self._concepts(ctx, chunks)
            await self._align(ctx)
            await self._misconceptions(ctx, chunks)
        except StepFailed as failed:
            async with self._uow:
                if not await self._uow.knowledge.touch_build(job_id, attempt):
                    return
                await self._uow.knowledge.set_build_status(section_id, "failed", None)
                await self._uow.jobs.mark_failed(job_id, failed.code, "section build failed")
            return
        except BuildSuperseded:
            return
        except Exception:
            async with self._uow:
                await self._uow.knowledge.release_build(job_id, attempt)
            raise
        async with self._uow:
            if not await self._uow.knowledge.touch_build(job_id, attempt):
                return
            await self._uow.knowledge.set_build_status(section_id, "built", self._clock.now())
            await self._uow.jobs.mark_succeeded(job_id)

    async def _record(self, ctx: BuildContext, invocations: Sequence[InvocationOut]) -> None:
        async with self._uow:
            await self._uow.ai_invocations.record(ctx.school_id, invocations)
        async with self._uow:
            await self._guard()

    async def _guard(self) -> None:
        if not await self._uow.knowledge.touch_build(self._job_id, self._attempt):
            raise BuildSuperseded()

    def _same_model(self, model: str | None) -> None:
        # AI-13: vectors from another model are not comparable with the stored ones.
        if model != self._settings.embedding_model:
            raise StepFailed("EMBEDDING_MODEL_MISMATCH")

    async def _chunks(self, ctx: BuildContext) -> list[ChunkRow]:
        if not ctx.has_chunks:
            pdf = await self._storage.download(ctx.storage_bucket, ctx.storage_path)
            spec = ChunkSpec(ctx.page_start, ctx.page_end, ctx.title, ctx.next_title)
            reply = await call_s1(
                lambda rid: self._ai.chunk_section(pdf, spec, rid),
                f"chunk-{ctx.section_id}",
                lambda inv: self._record(ctx, inv),
            )
            self._same_model(reply.result.embedding_model)
            async with self._uow:
                await self._guard()
                await self._uow.knowledge.insert_chunks(
                    ctx, reply.result.chunks, reply.result.embedding_model
                )
        async with self._uow:
            chunks = await self._uow.knowledge.section_chunks(ctx.section_id)
        if not chunks:
            raise StepFailed("SECTION_HAS_NO_TEXT")
        for chunk in chunks:
            self._same_model(chunk.embedding_model)
        return chunks

    async def _concepts(self, ctx: BuildContext, chunks: list[ChunkRow]) -> None:
        if ctx.has_concepts:
            return
        async with self._uow:
            await self._guard()
            existing = await self._uow.knowledge.kb_concepts(ctx.knowledge_base_id)
            edges = await self._uow.knowledge.kb_edges(ctx.knowledge_base_id)
            rejected = await self._uow.knowledge.rejected_concepts(ctx.knowledge_base_id)
        body = ExtractConceptsIn(
            chunks=[_chunk_ref(c) for c in chunks],
            existing_concepts=[
                ExistingConceptIn(id=i, name=n, description=d or "") for i, n, d in existing
            ],
            existing_prerequisites=[
                PrerequisiteEdgeIn(concept_id=a, prerequisite_concept_id=b) for a, b in edges
            ],
            rejected_concepts=[RejectedConceptIn(name=n, description=d or "") for n, d in rejected],
            section_title=ctx.title,
            subject=ctx.subject,
            phase=ctx.phase,
        )
        extracted = (
            await call_s1(
                lambda rid: self._ai.extract_concepts(body, rid),
                f"extract-{ctx.section_id}",
                lambda inv: self._record(ctx, inv),
            )
        ).result
        self._same_model(extracted.embedding_model)
        drafts = {d.key: d for d in extracted.concepts}
        chunk_ids = {c.id for c in chunks}
        existing_ids = {i for i, _, _ in existing}
        if (
            len(drafts) != len(extracted.concepts)
            or any(not set(d.source_chunk_ids) <= chunk_ids for d in drafts.values())
            or any(
                link.concept_id not in existing_ids or not set(link.source_chunk_ids) <= chunk_ids
                for link in extracted.existing_links
            )
        ):
            raise StepFailed("ai_output_invalid")
        links = [(link.concept_id, link.source_chunk_ids) for link in extracted.existing_links]
        creates = []
        linked_keys: dict[str, UUID] = {}
        if drafts:
            async with self._uow:
                items = [
                    DedupItemIn(
                        key=d.key,
                        name=d.name,
                        description=d.description,
                        candidates=await self._uow.knowledge.match_concepts(
                            ctx, d.embedding, self._settings.embedding_model
                        ),
                    )
                    for d in drafts.values()
                ]
            decisions = (
                await call_s1(
                    lambda rid: self._ai.dedupe_concepts(DedupeIn(items=items), rid),
                    f"dedupe-{ctx.section_id}",
                    lambda inv: self._record(ctx, inv),
                )
            ).result.decisions
            candidates = {i.key: {c.concept_id for c in i.candidates or []} for i in items}
            if (
                {d.key for d in decisions} != drafts.keys()
                or len(decisions) != len(drafts)
                or any(
                    d.action.value == "link" and d.existing_concept_id not in candidates[d.key]
                    for d in decisions
                )
            ):
                raise StepFailed("ai_output_invalid")
            for d in decisions:
                if d.action.value == "link" and d.existing_concept_id is not None:
                    links.append((d.existing_concept_id, drafts[d.key].source_chunk_ids))
                    linked_keys[d.key] = d.existing_concept_id
                elif d.action.value == "create":
                    creates.append(drafts[d.key])
        async with self._uow:
            await self._guard()
            await self._uow.knowledge.apply_concepts(
                ctx, links, creates, self._settings.embedding_model, linked_keys
            )

    async def _align(self, ctx: BuildContext) -> None:
        if ctx.cp_subject_id is None:
            return
        async with self._uow:
            todo = await self._uow.knowledge.concepts_to_align(ctx)
            for concept in todo:
                self._same_model(concept.embedding_model)
            items = [
                AlignItemIn(
                    concept_ref=str(c.id),
                    name=c.name,
                    description=c.description or "",
                    candidates=await self._uow.knowledge.match_cp(
                        ctx.cp_subject_id, c.embedding, self._settings.embedding_model
                    ),
                )
                for c in todo
            ]
        for batch in batched(items, 100):
            body = AlignCpIn(items=list(batch))
            result = (
                await call_s1(
                    partial(self._ai.align_cp, body),
                    f"align-{ctx.section_id}",
                    lambda inv: self._record(ctx, inv),
                )
            ).result
            candidates = {i.concept_ref: {c.outcome_id for c in i.candidates or []} for i in batch}
            if any(
                a.concept_ref not in candidates
                or (a.outcome_id is not None and a.outcome_id not in candidates[a.concept_ref])
                for a in result.alignments
            ):
                raise StepFailed("ai_output_invalid")
            async with self._uow:
                await self._guard()
                for a in result.alignments:
                    if a.outcome_id is not None:
                        await self._uow.knowledge.set_cp_outcome(
                            ctx, UUID(a.concept_ref), a.outcome_id
                        )

    async def _misconceptions(self, ctx: BuildContext, chunks: list[ChunkRow]) -> None:
        model = self._settings.embedding_model
        async with self._uow:
            todo = await self._uow.knowledge.concepts_without_misconceptions(ctx)
            for concept in todo:
                self._same_model(concept.embedding_model)
            library = {
                c.id: await self._uow.knowledge.match_library(ctx, c.embedding, model)
                if ctx.cp_subject_id
                else []
                for c in todo
            }
        refs = [_chunk_ref(c) for c in chunks]
        chunk_ids = {c.id for c in chunks}
        for batch in batched(todo, 30):
            body = GenerateMisconceptionsIn(
                chunks=refs,
                concepts=[
                    ConceptForMisconceptionsIn(
                        concept_ref=str(c.id),
                        name=c.name,
                        description=c.description or "",
                        library_candidates=library[c.id],
                        source_chunk_ids=[i for i in c.source_chunk_ids if i in chunk_ids][:50],
                    )
                    for c in batch
                ],
                subject=ctx.subject,
                phase=ctx.phase,
            )
            result = (
                await call_s1(
                    partial(self._ai.generate_misconceptions, body),
                    f"misconceptions-{ctx.section_id}",
                    lambda inv: self._record(ctx, inv),
                )
            ).result
            self._same_model(result.embedding_model)
            candidates = {str(c.id): {entry.library_id for entry in library[c.id]} for c in batch}
            if any(
                m.concept_ref not in candidates
                or not set(m.source_chunk_ids) <= chunk_ids
                or (m.library_id is not None and m.library_id not in candidates[m.concept_ref])
                for m in result.misconceptions
            ):
                raise StepFailed("ai_output_invalid")
            async with self._uow:
                await self._guard()
                await self._uow.knowledge.insert_misconceptions(ctx, result.misconceptions, model)
