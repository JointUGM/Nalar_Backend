from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import asyncpg

from nalar.application.ports.ai_contract import SectionOut
from nalar.application.ports.knowledge import (
    ConceptView,
    KbDetail,
    KbRef,
    KbSummary,
    MaterialForDetect,
    MaterialView,
    MisconceptionView,
    NewMaterial,
    SectionView,
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
