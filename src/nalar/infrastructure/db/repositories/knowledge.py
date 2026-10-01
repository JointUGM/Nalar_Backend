from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import asyncpg

from nalar.application.features.knowledge_base.s1_calls import BuildBusy
from nalar.application.ports.ai_contract import (
    CandidateIn,
    ChunkOut,
    ConceptDraftOut,
    CpCandidateIn,
    LibraryCandidateIn,
    MisconceptionOut,
    SectionOut,
)
from nalar.application.ports.knowledge import (
    BuildContext,
    ChunkRow,
    ConceptView,
    KbDetail,
    KbRef,
    KbSummary,
    MaterialForDetect,
    MaterialView,
    MisconceptionView,
    NewMaterial,
    SectionForBuild,
    SectionView,
    StoredConcept,
)
from nalar.infrastructure.db.pool import DbConnection

_KB_PAGE = """
    select kb.id, kb.topic_key, kb.topic_title, kb.school_subject_id, kb.owner_teacher_id,
           (select count(*) from teaching_materials m
             where m.knowledge_base_id = kb.id and m.archived_at is null)::int as material_count,
           (select count(*) from material_sections s
             where s.knowledge_base_id = kb.id and s.build_status = 'built')::int
               as built_section_count,
           ((select count(*) from concepts c where c.knowledge_base_id = kb.id
              and c.review_status = 'pending' and c.archived_at is null)
            + (select count(*) from misconceptions x where x.knowledge_base_id = kb.id
              and x.review_status = 'pending' and x.archived_at is null))::int as pending_count,
           (select count(*) from concepts c where c.knowledge_base_id = kb.id
             and c.review_status = 'approved' and c.archived_at is null)::int
               as approved_concept_count,
           kb.created_at
      from knowledge_bases kb
     where kb.school_id = $2
       and ($3::uuid is null or kb.school_subject_id = $3)
       and (kb.owner_teacher_id = $1 or exists (
             select 1 from teaching_assignments ta
              where ta.school_id = kb.school_id
                and ta.school_subject_id = kb.school_subject_id and ta.teacher_id = $1))
       and ($5::timestamptz is null or (kb.created_at, kb.id) < ($5, $6))
     order by kb.created_at desc, kb.id desc
     limit $4
"""


class PgKnowledgeRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def claim_build(
        self, ctx: BuildContext, job_id: UUID, now: datetime, stale_after_s: float
    ) -> int | None:
        await self._conn.execute(
            "select id from knowledge_bases where id = $1 for update", ctx.knowledge_base_id
        )
        job = await self._conn.fetchrow(
            "select status::text, entity_id, kind from jobs where id = $1 for update", job_id
        )
        if (
            job is None
            or job["status"] not in ("queued", "running")
            or job["entity_id"] != ctx.section_id
            or job["kind"] != "kb_build_section"
        ):
            return None
        busy = await self._conn.fetchval(
            "select exists (select 1 from jobs j join material_sections s on s.id = j.entity_id"
            " where j.kind = 'kb_build_section' and j.status = 'running'"
            " and s.knowledge_base_id = $1 and j.updated_at > $2)",
            ctx.knowledge_base_id,
            now - timedelta(seconds=stale_after_s),
        )
        if busy:
            raise BuildBusy()
        await self._conn.execute(
            "update jobs j set status = 'queued' from material_sections s"
            " where s.id = j.entity_id and s.knowledge_base_id = $1"
            " and j.kind = 'kb_build_section' and j.status = 'running'",
            ctx.knowledge_base_id,
        )
        attempt: int = await self._conn.fetchval(
            "update jobs set status = 'running', attempts = attempts + 1"
            " where id = $1 returning attempts",
            job_id,
        )
        return attempt

    async def touch_build(self, job_id: UUID, attempt: int) -> bool:
        await self._conn.execute(
            "select kb.id from knowledge_bases kb join material_sections s"
            " on s.knowledge_base_id = kb.id join jobs j on j.entity_id = s.id"
            " where j.id = $1 for update of kb",
            job_id,
        )
        return (
            await self._conn.fetchval(
                "update jobs set updated_at = now()"
                " where id = $1 and attempts = $2 and status = 'running' returning id",
                job_id,
                attempt,
            )
            is not None
        )

    async def release_build(self, job_id: UUID, attempt: int) -> None:
        await self._conn.execute(
            "update jobs set status = 'queued'"
            " where id = $1 and attempts = $2 and status = 'running'",
            job_id,
            attempt,
        )

    async def kb_ref(self, kb_id: UUID) -> KbRef | None:
        row = await self._conn.fetchrow(
            "select id, school_id, school_subject_id, owner_teacher_id from knowledge_bases"
            " where id = $1",
            kb_id,
        )
        return KbRef(**dict(row)) if row else None

    async def topic_exists(self, school_subject_id: UUID, key: str) -> bool:
        return bool(
            await self._conn.fetchval(
                "select exists (select 1 from knowledge_bases"
                " where school_subject_id = $1 and topic_key = $2)",
                school_subject_id,
                key,
            )
        )

    async def create_kb(self, kb: KbRef, key: str, title: str) -> bool:
        try:
            async with self._conn.transaction():
                await self._conn.execute(
                    "insert into knowledge_bases (id, school_id, school_subject_id,"
                    " owner_teacher_id, topic_key, topic_title) values ($1, $2, $3, $4, $5, $6)",
                    kb.id,
                    kb.school_id,
                    kb.school_subject_id,
                    kb.owner_teacher_id,
                    key,
                    title,
                )
        except asyncpg.UniqueViolationError:
            return False
        return True

    async def add_material(self, kb: KbRef, uploader_id: UUID, material: NewMaterial) -> None:
        await self._conn.execute(
            "insert into teaching_materials (id, school_id, knowledge_base_id, uploaded_by, title,"
            " storage_path, mime_type, size_bytes)"
            " values ($1, $2, $3, $4, $5, $6, 'application/pdf', $7)",
            material.id,
            kb.school_id,
            kb.id,
            uploader_id,
            material.title,
            material.storage_path,
            material.size_bytes,
        )

    async def material_for_detect(self, material_id: UUID) -> MaterialForDetect | None:
        row = await self._conn.fetchrow(
            "select id, school_id, knowledge_base_id, title, storage_bucket, storage_path"
            "  from teaching_materials where id = $1 and archived_at is null",
            material_id,
        )
        return MaterialForDetect(**dict(row)) if row else None

    async def has_sections(self, material_id: UUID) -> bool:
        return bool(
            await self._conn.fetchval(
                "select exists (select 1 from material_sections where material_id = $1)",
                material_id,
            )
        )

    async def store_detection(
        self,
        material: MaterialForDetect,
        page_count: int,
        pages_without_text: Sequence[int],
        sections: Sequence[SectionOut],
    ) -> None:
        ids: dict[int, UUID] = {}
        for s in sorted(sections, key=lambda s: s.ordinal):
            ids[s.ordinal] = await self._conn.fetchval(
                "insert into material_sections (school_id, knowledge_base_id, material_id,"
                " parent_section_id, ordinal, level, title, page_start, page_end, suggested)"
                " values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) returning id",
                material.school_id,
                material.knowledge_base_id,
                material.id,
                ids.get(s.parent_ordinal) if s.parent_ordinal is not None else None,
                s.ordinal,
                s.level,
                s.title,
                s.page_start,
                s.page_end,
                s.suggested,
            )
        await self._conn.execute(
            "update teaching_materials set page_count = $2, pages_without_text = $3,"
            " processing_status = 'processed' where id = $1",
            material.id,
            page_count,
            list(pages_without_text),
        )

    async def mark_material_failed(self, material_id: UUID) -> None:
        await self._conn.execute(
            "update teaching_materials set processing_status = 'failed' where id = $1", material_id
        )

    async def kb_page(
        self,
        actor_id: UUID,
        school_id: UUID,
        school_subject_id: UUID | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[KbSummary]:
        rows = await self._conn.fetch(
            _KB_PAGE,
            actor_id,
            school_id,
            school_subject_id,
            limit,
            after[0] if after else None,
            after[1] if after else None,
        )
        return [KbSummary(**dict(r)) for r in rows]

    async def kb_detail(self, kb_id: UUID, approved_only: bool) -> KbDetail | None:
        head = await self._conn.fetchrow(
            "select id, topic_title, owner_teacher_id from knowledge_bases where id = $1", kb_id
        )
        if head is None:
            return None
        visible = "and review_status = 'approved'" if approved_only else ""
        materials = await self._conn.fetch(
            "select id, title, page_count, pages_without_text, archived_at from teaching_materials"
            " where knowledge_base_id = $1 order by created_at",
            kb_id,
        )
        concepts = await self._conn.fetch(
            "select id, name, description, review_status::text as review_status,"
            " cp_learning_outcome_id, source_chunk_ids from concepts"
            f" where knowledge_base_id = $1 and archived_at is null {visible} order by created_at",
            kb_id,
        )
        edges = await self._conn.fetch(
            "select concept_id, prerequisite_concept_id from concept_prerequisites"
            " where knowledge_base_id = $1",
            kb_id,
        )
        misconceptions = await self._conn.fetch(
            "select id, concept_id, statement, correct_understanding, detection_cues,"
            " counter_examples, review_status::text as review_status, source_chunk_ids"
            f" from misconceptions where knowledge_base_id = $1 and archived_at is null {visible}"
            " order by created_at",
            kb_id,
        )
        shown = {c["id"] for c in concepts}
        return KbDetail(
            id=head["id"],
            topic_title=head["topic_title"],
            owner_teacher_id=head["owner_teacher_id"],
            materials=tuple(
                MaterialView(
                    m["id"],
                    m["title"],
                    m["page_count"],
                    tuple(m["pages_without_text"]),
                    m["archived_at"],
                )
                for m in materials
            ),
            concepts=tuple(
                ConceptView(
                    c["id"],
                    c["name"],
                    c["description"],
                    c["review_status"],
                    c["cp_learning_outcome_id"],
                    tuple(c["source_chunk_ids"]),
                )
                for c in concepts
            ),
            prerequisites=tuple(
                (e["concept_id"], e["prerequisite_concept_id"])
                for e in edges
                if e["concept_id"] in shown and e["prerequisite_concept_id"] in shown
            ),
            misconceptions=tuple(
                MisconceptionView(
                    x["id"],
                    x["concept_id"],
                    x["statement"],
                    x["correct_understanding"],
                    tuple(x["detection_cues"]),
                    tuple(x["counter_examples"]),
                    x["review_status"],
                    tuple(x["source_chunk_ids"]),
                )
                for x in misconceptions
            ),
        )

    async def sections(self, kb_id: UUID) -> list[SectionView]:
        rows = await self._conn.fetch(
            "select s.id, s.material_id, s.parent_section_id, s.ordinal, s.level, s.title,"
            " s.page_start, s.page_end, s.build_status::text as build_status, s.built_at,"
            " s.suggested from material_sections s"
            " join teaching_materials m on m.id = s.material_id"
            " where s.knowledge_base_id = $1 and m.archived_at is null"
            " order by m.created_at, s.ordinal",
            kb_id,
        )
        return [SectionView(**dict(r)) for r in rows]

    async def section_for_build(self, section_id: UUID) -> SectionForBuild | None:
        await self._conn.execute(
            "select kb.id from knowledge_bases kb join material_sections s"
            " on s.knowledge_base_id = kb.id where s.id = $1 for update of kb",
            section_id,
        )
        await self._conn.execute(
            "select 1 from material_sections where material_id ="
            " (select material_id from material_sections where id = $1) order by id for update",
            section_id,
        )
        row = await self._conn.fetchrow(
            "select s.id, s.knowledge_base_id, s.school_id, s.build_status::text as build_status,"
            "       j.id as job_id, j.updated_at as job_updated_at"
            "  from material_sections s"
            "  left join lateral (select id, updated_at from jobs"
            "        where kind = 'kb_build_section' and entity_id = s.id"
            "        and status in ('queued', 'running')"
            "        order by created_at desc limit 1) j on true"
            " where s.id = $1 and exists (select 1 from teaching_materials m"
            " where m.id = s.material_id and m.archived_at is null)",
            section_id,
        )
        return SectionForBuild(**dict(row)) if row else None

    async def overlaps(self, section_id: UUID) -> bool:
        return bool(
            await self._conn.fetchval(
                "with recursive ancestors as ("
                " select parent_section_id as id from material_sections where id = $1"
                " union select s.parent_section_id from material_sections s"
                " join ancestors a on s.id = a.id), descendants as ("
                " select id from material_sections where parent_section_id = $1"
                " union select s.id from material_sections s"
                " join descendants d on s.parent_section_id = d.id)"
                " select exists (select 1 from material_sections s"
                " where s.id in (select id from ancestors union select id from descendants)"
                " and s.build_status in ('queued', 'building', 'built'))",
                section_id,
            )
        )

    async def queue_section(self, section_id: UUID) -> None:
        await self._conn.execute(
            "update jobs set status = 'failed', error_code = 'BUILD_SUPERSEDED'"
            " where entity_type = 'material_sections' and entity_id = $1"
            " and status in ('queued', 'running')",
            section_id,
        )
        await self._conn.execute(
            "update material_sections set build_status = 'queued' where id = $1", section_id
        )

    async def build_context(self, section_id: UUID, default_phase: str) -> BuildContext | None:
        row = await self._conn.fetchrow(
            """
            select s.id as section_id, s.school_id, s.knowledge_base_id, s.ordinal, s.title,
                   s.page_start, s.page_end,
                   (select n.title from material_sections n
                     where n.material_id = s.material_id and n.ordinal > s.ordinal
                       and n.level <= s.level order by n.ordinal limit 1) as next_title,
                   m.storage_bucket, m.storage_path,
                   coalesce(cs.name, ss.name) as subject,
                   coalesce(cs.phase, $2) as phase,
                   ss.cp_subject_id,
                   exists (select 1 from material_chunks c where c.section_id = s.id) as has_chunks,
                   s.concepts_built_at is not null as has_concepts
              from material_sections s
              join teaching_materials m on m.id = s.material_id
              join knowledge_bases kb on kb.id = s.knowledge_base_id
              join school_subjects ss on ss.id = kb.school_subject_id
              left join cp_subjects cs on cs.id = ss.cp_subject_id
             where s.id = $1 and m.archived_at is null
            """,
            section_id,
            default_phase,
        )
        return BuildContext(**dict(row)) if row else None

    async def set_build_status(
        self, section_id: UUID, status: str, built_at: datetime | None
    ) -> None:
        await self._conn.execute(
            "update material_sections set build_status = $2::section_build_status,"
            " built_at = coalesce($3, built_at) where id = $1",
            section_id,
            status,
            built_at,
        )

    async def insert_chunks(
        self, ctx: BuildContext, chunks: Sequence[ChunkOut], embedding_model: str
    ) -> None:
        # INTEGRATION.md step 3: chunk_index = ordinal × 10 000 + local_index keeps sections apart.
        await self._conn.executemany(
            "insert into material_chunks (school_id, material_id, knowledge_base_id, section_id,"
            " chunk_index, content, token_count, embedding, embedding_model, page_start, page_end,"
            " heading_path, chunk_kind)"
            " select $1, s.material_id, $2, s.id, $4, $5, $6, $7, $8, $9, $10, $11,"
            " $12::chunk_kind from material_sections s where s.id = $3"
            " on conflict (material_id, chunk_index) do nothing",
            [
                (
                    ctx.school_id,
                    ctx.knowledge_base_id,
                    ctx.section_id,
                    ctx.ordinal * 10_000 + c.local_index,
                    c.content,
                    c.token_count,
                    c.embedding,
                    embedding_model,
                    c.page_start,
                    c.page_end,
                    c.heading_path,
                    c.chunk_kind.value,
                )
                for c in chunks
            ],
        )

    async def section_chunks(self, section_id: UUID) -> list[ChunkRow]:
        rows = await self._conn.fetch(
            "select id, content, heading_path, chunk_kind::text as kind, page_start, page_end,"
            " embedding_model"
            "  from material_chunks where section_id = $1 order by chunk_index",
            section_id,
        )
        return [ChunkRow(**dict(r)) for r in rows]

    async def kb_concepts(self, kb_id: UUID) -> list[tuple[UUID, str, str | None]]:
        rows = await self._conn.fetch(
            "select id, name, description from concepts where knowledge_base_id = $1"
            " and archived_at is null and review_status <> 'rejected' order by created_at",
            kb_id,
        )
        return [(r["id"], r["name"], r["description"]) for r in rows]

    async def kb_edges(self, kb_id: UUID) -> list[tuple[UUID, UUID]]:
        rows = await self._conn.fetch(
            "select concept_id, prerequisite_concept_id from concept_prerequisites"
            " where knowledge_base_id = $1",
            kb_id,
        )
        return [(r["concept_id"], r["prerequisite_concept_id"]) for r in rows]

    async def rejected_concepts(self, kb_id: UUID) -> list[tuple[str, str | None]]:
        rows = await self._conn.fetch(
            "select name, description from concepts where knowledge_base_id = $1"
            " and review_status = 'rejected'",
            kb_id,
        )
        return [(r["name"], r["description"]) for r in rows]

    async def match_concepts(
        self, ctx: BuildContext, embedding: Sequence[float], model: str
    ) -> list[CandidateIn]:
        rows = await self._conn.fetch(
            "select concept_id, name, description, similarity"
            "  from match_kb_concepts($1, $2, $3, $4, 5)",
            ctx.school_id,
            ctx.knowledge_base_id,
            list(embedding),
            model,
        )
        return [
            CandidateIn(
                concept_id=r["concept_id"],
                name=r["name"],
                description=r["description"] or "",
                similarity=max(-1.0, min(1.0, r["similarity"])),
            )
            for r in rows
        ]

    async def apply_concepts(
        self,
        ctx: BuildContext,
        links: Sequence[tuple[UUID, Sequence[UUID]]],
        creates: Sequence[ConceptDraftOut],
        model: str,
        linked_keys: dict[str, UUID] | None = None,
    ) -> None:
        for concept_id, chunk_ids in links:
            await self._conn.execute(
                "update concepts set source_chunk_ids = array(select distinct unnest("
                " source_chunk_ids || $3::uuid[])) where id = $1 and knowledge_base_id = $2",
                concept_id,
                ctx.knowledge_base_id,
                list(chunk_ids),
            )
        created: dict[str, UUID] = dict(linked_keys or {})
        for draft in creates:
            created[draft.key] = await self._conn.fetchval(
                "insert into concepts (school_id, knowledge_base_id, name, description, origin,"
                " source_chunk_ids, embedding, embedding_model)"
                " values ($1, $2, $3, $4, 'ai_generated', $5, $6, $7) returning id",
                ctx.school_id,
                ctx.knowledge_base_id,
                draft.name,
                draft.description,
                list(draft.source_chunk_ids),
                draft.embedding,
                model,
            )
        for draft in creates:
            prerequisites = [created[k] for k in draft.prerequisite_keys if k in created]
            prerequisites += list(draft.prerequisite_existing_ids)
            await self._conn.executemany(
                "insert into concept_prerequisites"
                " (knowledge_base_id, concept_id, prerequisite_concept_id)"
                " select $1::uuid, $2::uuid, $3::uuid where $2::uuid <> $3::uuid"
                " and exists (select 1 from concepts"
                "   where id = $3 and knowledge_base_id = $1)"
                " on conflict do nothing",
                [(ctx.knowledge_base_id, created[draft.key], p) for p in prerequisites],
            )
        await self._conn.execute(
            "update material_sections set concepts_built_at = now() where id = $1", ctx.section_id
        )

    async def _section_concepts(self, ctx: BuildContext, extra: str) -> list[StoredConcept]:
        rows = await self._conn.fetch(
            "select c.id, c.name, c.description, c.embedding, c.source_chunk_ids, c.embedding_model"
            "  from concepts c where c.knowledge_base_id = $1 and c.origin = 'ai_generated'"
            "   and c.review_status = 'pending' and c.archived_at is null"
            "   and c.embedding is not null"
            "   and c.source_chunk_ids && array(select id from material_chunks"
            "                                     where section_id = $2)"
            f"  {extra} order by c.created_at",
            ctx.knowledge_base_id,
            ctx.section_id,
        )
        return [
            StoredConcept(
                r["id"],
                r["name"],
                r["description"],
                r["embedding"].to_list(),
                tuple(r["source_chunk_ids"]),
                r["embedding_model"],
            )
            for r in rows
        ]

    async def concepts_to_align(self, ctx: BuildContext) -> list[StoredConcept]:
        return await self._section_concepts(ctx, "and c.cp_learning_outcome_id is null")

    async def concepts_without_misconceptions(self, ctx: BuildContext) -> list[StoredConcept]:
        return await self._section_concepts(
            ctx, "and not exists (select 1 from misconceptions x where x.concept_id = c.id)"
        )

    async def match_cp(
        self, cp_subject_id: UUID, embedding: Sequence[float], model: str
    ) -> list[CpCandidateIn]:
        rows = await self._conn.fetch(
            "select outcome_id, element, description, similarity"
            "  from match_cp_outcomes($1, $2, $3, 5)",
            cp_subject_id,
            list(embedding),
            model,
        )
        return [
            CpCandidateIn(
                outcome_id=r["outcome_id"],
                element=r["element"],
                description=r["description"],
                similarity=max(-1.0, min(1.0, r["similarity"])),
            )
            for r in rows
        ]

    async def set_cp_outcome(self, ctx: BuildContext, concept_id: UUID, outcome_id: UUID) -> None:
        await self._conn.execute(
            "update concepts set cp_learning_outcome_id = $3 where id = $1"
            " and knowledge_base_id = $2",
            concept_id,
            ctx.knowledge_base_id,
            outcome_id,
        )

    async def match_library(
        self, ctx: BuildContext, embedding: Sequence[float], model: str
    ) -> list[LibraryCandidateIn]:
        rows = await self._conn.fetch(
            "select library_id, statement, correct_understanding, student_phrasings,"
            " counter_examples, similarity"
            "  from match_misconception_library($1, $2, $3, $4, 5)",
            ctx.subject,
            ctx.phase,
            list(embedding),
            model,
        )
        return [
            LibraryCandidateIn(
                library_id=r["library_id"],
                statement=r["statement"],
                correct_understanding=r["correct_understanding"],
                student_phrasings=list(r["student_phrasings"]),
                counter_examples=list(r["counter_examples"]),
                similarity=max(-1.0, min(1.0, r["similarity"])),
            )
            for r in rows
        ]

    async def insert_misconceptions(
        self, ctx: BuildContext, rows: Sequence[MisconceptionOut], model: str
    ) -> None:
        await self._conn.executemany(
            "insert into misconceptions (school_id, knowledge_base_id, concept_id, statement,"
            " correct_understanding, origin, detection_cues, counter_examples, source_chunk_ids,"
            " library_id, embedding, embedding_model)"
            " select $1, $2, c.id, $4, $5, 'ai_generated', $6, $7, $8, $9, $10, $11"
            "   from concepts c where c.id = $3 and c.knowledge_base_id = $2",
            [
                (
                    ctx.school_id,
                    ctx.knowledge_base_id,
                    UUID(m.concept_ref),
                    m.statement,
                    m.correct_understanding,
                    list(m.detection_cues),
                    list(m.counter_examples),
                    list(m.source_chunk_ids),
                    m.library_id,
                    m.embedding,
                    model,
                )
                for m in rows
            ],
        )
